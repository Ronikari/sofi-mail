# Оркестрация письма: разбор -> отсев -> заявка в журнале -> сессия -> вложения -> под замком сессии: запрос к модели -> ответ -> запись реплик; демон опрашивает ящик пулом потоков и раз в сутки чистит по срокам хранения.
# once и serve вызывают одну process_email, поэтому поведение при отладке совпадает с демоном.

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.message import Message
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from src import attachment_context, redact, storage
from src.config import (
    ATTACHMENT_RETENTION_DAYS,
    LLM_TIMEOUT_SEC,
    MAIL_ADDRESS,
    MARK_SEEN,
    MAX_HISTORY_MESSAGES,
    MAX_PROMPT_CHARS,
    POLL_INTERVAL_SEC,
    RATE_LIMIT_PER_HOUR,
    RETENTION_DAYS,
    STORE_RAW_BODY,
    THREAD_BY_SUBJECT,
    WORKERS,
    is_sender_allowed,
)
from src.email_parser import IncomingEmail, automated_reason, parse_email
from src.transport import MailTransport, get_transport

log = logging.getLogger(__name__)

T = TypeVar("T")

# Замок вокруг «найти либо создать» сессию: без него два письма одной темы из одного прохода создали бы по сессии каждое.
# Замок процессный — демон рассчитан на один процесс, несколько экземпляров на одну базу он не защитит.
_session_create_lock = threading.Lock()

# Замки на сессию сериализуют реплики одной сессии; без них два письма треда прочитали бы одну и ту же историю и потеряли бы второй вопрос.
# Под замком — только чтение истории, запрос к модели, отправка и запись; загрузка вложений (минуты) идёт до него.
_session_locks_guard = threading.Lock()
_session_locks: Dict[int, threading.Lock] = {}


# вход: id сессии (0 — примерка без сессии). выход: общий замок сессии.
# setdefault под _session_locks_guard создаёт его один раз — иначе два потока получили бы разные объекты на одну сессию.
def _session_lock(session_id: int) -> threading.Lock:
    """Отдаёт замок, сериализующий обработку писем одной сессии."""
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


# вход: id сессий, которые run_retention только что удалил из базы; чистит их замки в _session_locks.
# Без этого словарь рос бы весь срок жизни процесса; занятый замок здесь не трогается, чтобы не мешать сессии, которую как раз дообрабатывает поток.
def _prune_session_locks(session_ids: List[int]) -> None:
    """Убирает из _session_locks замки только что удалённых сессий."""
    if not session_ids:
        return

    with _session_locks_guard:
        for session_id in session_ids:
            lock = _session_locks.get(session_id)
            if lock is not None and not lock.locked():
                del _session_locks[session_id]


# текст письма пользователю, когда вопрос в теле письма не найден
EMPTY_BODY_NOTICE = (
    "В письме не нашлось текста вопроса — возможно, он целиком состоял из цитаты "
    "или вложения. Сформулируйте вопрос в теле письма."
)
# тот же случай для письма в формате RTF: решение здесь другое, поэтому
# и подсказка своя
TNEF_NOTICE = (
    "Письмо пришло в формате RTF (текст упакован в вложение winmail.dat), поэтому "
    "прочитать вопрос не удалось. Переключите формат письма на «Обычный текст» "
    "или HTML в настройках Outlook и отправьте вопрос заново."
)
# Пересылка без текста от себя: тело — чужой тред без указания авторства реплик.
# Ответ по нему опирался бы на переписку посторонних людей и лёг бы в историю репликой пересылающего.
FORWARD_NO_TEXT_NOTICE = (
    "Письмо переслано без текста от Вас — в нём только переписка других людей. "
    "Сформулируйте вопрос в теле письма: что именно необходимо посмотреть в переписке."
)
# вопрос за пользователя для письма без текста с приложенным документом:
# модель получила бы документ и пустую строку
DOCUMENT_ONLY_PROMPT = (
    "Письмо пришло без текста, только с приложенным документом. Проанализируйте документ."
)
RATE_LIMIT_NOTICE = (
    "Превышен лимит писем в час ({limit}). Ответы возобновятся автоматически позже."
)
LLM_ERROR_NOTICE = (
    "Не удалось получить ответ модели: {error}\n\n"
    "Письмо сохранено — после восстановления сервиса его можно обработать "
    "повторно командой `python -m src.cli retry`."
)
TRUNCATION_NOTICE = (
    "\n\n(Примечание: письмо было длиннее {limit} символов и обработано частично.)"
)
# Предупреждение о свёртке — ставится первой строкой ответа и при сводке по просьбе, и когда сессия свернулась по SESSION_MAX_CHARS.
# Объясняет то, что не видно в клиенте: письма остаются в цепочке, но выпадают из контекста модели.
SUMMARY_DEGRADATION_NOTICE = (
    "История переписки этой сессии была подвергнута суммаризации — "
    "возможна деградация исторического контекста."
)
# ответ на просьбу свернуть переписку, в которой ещё нет ни одной реплики
SUMMARY_EMPTY_NOTICE = (
    "Сессия пуста, отсутствует информация для суммаризации."
)


# итог обработки одного письма, возвращается вызывающему коду
@dataclass
class Outcome:
    status: str  # ok | skipped | error
    detail: str = ""
    session_id: Optional[int] = None

    # выход: True для писем, которые можно пометить прочитанными.
    # error остаётся непрочитанным — retry находит его в папке по Message-ID.
    @property
    def can_mark_seen(self) -> bool:
        """Сообщает, можно ли отметить письмо обработанным."""
        return self.status in ("ok", "skipped")


# счётчики одного прохода по ящику, печатает их команда cli once
@dataclass
class RunSummary:
    fetched: int = 0
    answered: int = 0
    skipped: int = 0
    failed: int = 0

    # вход: итог обработки одного письма.
    # побочный эффект: увеличение счётчиков
    def add(self, outcome: Outcome) -> None:
        """Учитывает итог одного письма в счётчиках прохода."""
        self.fetched += 1
        if outcome.status == "ok":
            self.answered += 1
        elif outcome.status == "skipped":
            self.skipped += 1
        # прочие статусы означают ошибку обработки
        else:
            self.failed += 1


# Повторяет action до attempts раз с удвоением паузы; последнее исключение пробрасывается наружу.
# Годится только для идемпотентных операций — запрос к модели можно повторить, отправку письма через Exchange нельзя.
def _retry(action: Callable[[], T], attempts: int, what: str) -> T:
    """Повторяет операцию с удвоением паузы между попытками."""
    # начальная пауза в секундах
    delay = 2.0

    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            # последняя попытка отдаёт исключение вызывающему коду
            if attempt == attempts:
                raise

            log.warning("%s: попытка %d/%d не удалась (%s), повтор через %.0f с", what, attempt, attempts, exc, delay)
            time.sleep(delay)
            delay *= 2

    # цикл выходит только через return либо raise; строка защищает от правки,
    # меняющей условия выше
    raise AssertionError("недостижимо")


# Отправляет письмо через Exchange либо печатает в консоль (dry_run); выход — Message-ID или None.
# Вызывается один раз на письмо: повтор при неясном исходе отправки дал бы получателю второе письмо.
def _reply(
    incoming: IncomingEmail,
    title: str,
    body: str,
    transport: MailTransport,
    dry_run: bool,
) -> Optional[str]:
    """Отправляет письмо пользователю либо печатает его в консоль."""
    # режим примерки состояние не меняет: письмо уходит на экран
    if dry_run:
        print(f"\n--- [dry-run] ответ для {incoming.sender} (сессия «{title}») ---\n{body}\n---\n")
        return None

    # Заголовки треда/разговора продолжают тред Outlook; quoted_body берёт incoming.body_raw (тело целиком с цепочкой цитат), а не body —
    # иначе в клиенте терялась бы вся история, кроме последней реплики. quoted_html несёт html-разметку как есть для цитаты; в модель и в messages идёт только очищенный incoming.body.
    return transport.send_reply(
        to_address=incoming.sender,
        subject=incoming.subject,
        body=body,
        session_title=title,
        in_reply_to=incoming.message_id,
        references=incoming.references,
        thread_index=incoming.thread_index,
        incoming_topic=incoming.thread_topic,
        sender_name=incoming.sender_name,
        quoted_body=incoming.body_raw,
        sent_date=incoming.date,
        quoted_html=incoming.body_html,
    )


# вход: разобранное письмо. выход: id существующей сессии либо None.
# Оба поиска ограничены адресом отправителя — сессия описывает переписку с одним человеком.
def _find_session(incoming: IncomingEmail) -> Optional[int]:
    """Ищет сессию письма по заголовкам треда, затем по теме."""
    # Заголовки треда проверяются первыми (тема повторяется, Message-ID уникален); собственный id письма стоит перед предками.
    # Иначе письмо, повторно разобранное после сбоя отправки, открыло бы новую сессию, и ответ разошёлся бы с вопросом.
    session_id = storage.find_session_by_message_ids(
        [incoming.message_id, *incoming.ancestor_ids], incoming.sender
    )
    if session_id:
        log.debug("сессия %s найдена по заголовкам треда", session_id)
        return session_id

    # поиск по теме включается флагом THREAD_BY_SUBJECT и работает для клиентов,
    # не проставивших заголовки треда
    if THREAD_BY_SUBJECT:
        session_id = storage.find_session_by_subject(incoming.sender, incoming.title)
        if session_id:
            log.debug("сессия %s найдена по теме %s", session_id, redact.subject(incoming.title))
            return session_id

    return None


# вход: исходное сообщение и его разбор. выход: текст причины отказа, None — если письмо годится в работу.
# Отправитель об отказе не уведомляется.
def _rejection_reason(msg: Message, incoming: IncomingEmail) -> Optional[str]:
    """Определяет, оставить ли письмо без обработки и без ответа."""
    # автоответчики, рассылки и собственные письма отсекает email_parser
    automated = automated_reason(msg, MAIL_ADDRESS)
    if automated:
        return automated

    # адрес вне ALLOWED_SENDERS остаётся без ответа: письмо в ответ подтвердило
    # бы отправителю рабочее состояние ящика
    if not is_sender_allowed(incoming.sender):
        return f"отправитель {redact.email_addr(incoming.sender)} не в ALLOWED_SENDERS"

    return None


# Обрабатывает письмо от разбора до ответа; исключения наружу не выпускаются, итог — Outcome(ok|skipped|error).
# Вызывается из нескольких потоков параллельно — состояние держится в sqlite: заявка атомарна, порядок реплик — замками.
def process_email(
    msg: Message, dry_run: bool = False, transport: Optional[MailTransport] = None
) -> Outcome:
    """Обрабатывает одно письмо от разбора до отправки ответа."""
    transport = transport or get_transport()
    incoming = parse_email(msg)

    log.info(
        "письмо от %s: %s", redact.email_addr(incoming.sender), redact.subject(incoming.subject)
    )

    # письма автоответчиков и посторонних отправителей уходят без ответа
    reason = _rejection_reason(msg, incoming)
    if reason:
        log.info("пропущено: %s", reason)
        return Outcome("skipped", reason)

    # Заявка ставится до обращения к модели: перезапуск демона в процессе обработки не даёт второго ответа на то же письмо.
    # В режиме примерки заявка не ставится — иначе она заняла бы письмо и обычный запуск его пропустил бы.
    if not dry_run and not storage.claim_message(incoming.message_id):
        log.debug("письмо %s уже обработано", incoming.message_id)
        return Outcome("skipped", "уже в журнале обработки")

    try:
        return _process_claimed(incoming, transport, dry_run)

    # ветка перехвата: исключение на одном письме останавливает обработку
    # только этого письма, цикл прохода продолжается
    except Exception as exc:
        log.exception("необработанная ошибка на письме %s", incoming.message_id)
        if not dry_run:
            storage.finish_message(incoming.message_id, "error", str(exc))
        return Outcome("error", str(exc))


# вход: письмо и dry_run. выход: id сессии (0 — примерка нового треда, сессия не создаётся).
# Поиск и создание — под одним замком: иначе два письма одной темы из одного прохода создали бы две сессии.
def _resolve_session(incoming: IncomingEmail, dry_run: bool) -> int:
    """Находит сессию письма либо заводит новую."""
    with _session_create_lock:
        existing = _find_session(incoming)
        if existing:
            return existing

        # примерка нового треда сессию не создаёт: состояние базы остаётся
        # прежним, и прогон повторяется с тем же результатом
        if dry_run:
            return 0

        session_id = storage.create_session(incoming.title, incoming.sender)
        log.info(
            "новая сессия %s: %s с %s",
            session_id, redact.subject(incoming.title), redact.email_addr(incoming.sender),
        )
        return session_id


# вход: письмо, транспорт, dry_run; предусловие — письмо застолблено storage.claim_message.
# В режиме примерки состояние не меняется — прогон повторяется с тем же результатом.
def _process_claimed(incoming: IncomingEmail, transport: MailTransport, dry_run: bool) -> Outcome:
    """Ведёт письмо от проверки лимитов до ответа модели."""

    # закрывает заявку в журнале; в режиме примерки заявки нет
    def record(status: str, detail: str) -> None:
        if not dry_run:
            storage.finish_message(incoming.message_id, status, detail)

    # ограничение частоты считается по всем сессиям адреса за последний час
    if storage.count_messages_last_hour(incoming.sender) >= RATE_LIMIT_PER_HOUR:
        _reply(incoming, incoming.title, RATE_LIMIT_NOTICE.format(limit=RATE_LIMIT_PER_HOUR), transport, dry_run)
        record("skipped", "rate limit")
        return Outcome("skipped", "превышен лимит писем в час")

    prompt = incoming.body

    # письмо без текста и без вложений: обрабатывать нечего
    if not prompt and not incoming.attachments:
        # три причины пустого тела, у каждой свой ответ: пересылка чужого треда
        # без вопроса, формат RTF, письмо из одной цитаты
        if incoming.is_forward:
            notice, detail = FORWARD_NO_TEXT_NOTICE, "пересылка без текста от отправителя"
        elif incoming.is_tnef:
            notice, detail = TNEF_NOTICE, "тело в winmail.dat"
        else:
            notice, detail = EMPTY_BODY_NOTICE, "пустое тело письма"

        _reply(incoming, incoming.title, notice, transport, dry_run)
        record("skipped", detail)
        return Outcome("skipped", "в письме нет текста")

    # письмо из одних вложений встречается часто: строку «см. вложение» пишут
    # не всегда. вопрос подставляется готовым текстом
    if not prompt:
        prompt = DOCUMENT_ONLY_PROMPT

    # Обрезка длинного письма (TRUNCATION_NOTICE предупреждает); MAX_PROMPT_CHARS=0 отключает её — единственный предел на размер письма.
    # Свёртка сессии не режет текст самого письма; сверх предела объём нигде не режется.
    truncated = MAX_PROMPT_CHARS > 0 and len(prompt) > MAX_PROMPT_CHARS
    if truncated:
        log.warning("письмо длиной %d символов обрезано до %d", len(prompt), MAX_PROMPT_CHARS)
        prompt = prompt[:MAX_PROMPT_CHARS]

    session_id = _resolve_session(incoming, dry_run)
    session = storage.get_session(session_id) if session_id else None

    # название существующей сессии сохраняется: письмо продолжает её тред
    # под прежним заголовком
    title = session["title"] if session else incoming.title

    # Загрузка вложений идёт до замка сессии: она может занять минуты, а письма того же треда без документов не должны их ждать.
    upload = attachment_context.upload_attachments(incoming, dry_run)

    try:
        # замок сессии сериализует реплики одного треда; разные сессии
        # обрабатываются параллельно
        with _session_lock(session_id):
            # Запись/чтение файлов — под тем же замком сессии: письма одного треда, пришедшие разом, иначе перемешали бы свои файлы.
            # Второе письмо увидело бы в истории документ, про который его не спрашивали.
            attachments_ctx = attachment_context.build_context(
                incoming, session_id, upload, dry_run
            )

            # Вложение отвергнуто и до модели не дошёл ни один файл: ответ по одному тексту был бы не на заданный вопрос, поэтому запроса к модели не происходит.
            # Ветка стоит до проверки пустого тела — она срабатывает и на письме с текстом, где EMPTY_BODY_NOTICE неприменим.
            if upload.rejected and not attachments_ctx.files:
                notice = attachments_ctx.standalone_notice
                _reply(incoming, title, notice, transport, dry_run)
                record("skipped", "вложение отклонено проверкой: " + "; ".join(upload.rejected))
                return Outcome("skipped", "вложение отклонено", session_id)

            # Письмо из одних вложений, ни одно не дошло до модели: запрос без вопроса и без документа дал бы ответ по пустому месту.
            if not incoming.body and not attachments_ctx.files:
                notice = attachments_ctx.standalone_notice or EMPTY_BODY_NOTICE
                _reply(incoming, title, notice, transport, dry_run)
                record("skipped", "вложения не обработаны, текста в письме нет")
                return Outcome("skipped", "вложения не обработаны", session_id)

            return _answer(
                incoming, session_id, title, prompt, truncated, attachments_ctx,
                transport, dry_run, record,
            )
    finally:
        # Уборка — в finally: файл примерки не переживает прогон ни на одной ветке выхода, а записи о нём в базе нет — удалить будет нечем.
        # Список берётся из результата загрузки (заполнен до замка), сбой в build_context уборку не отменяет.
        if dry_run:
            attachment_context.drop_uploaded(upload.file_ids)


# Запрашивает ответ модели и отправляет письмо; побочные эффекты — запрос к модели, отправка, запись реплик.
# Предусловие: вызывается под замком сессии.
def _answer(
    incoming: IncomingEmail,
    session_id: int,
    title: str,
    prompt: str,
    truncated: bool,
    attachments_ctx: "attachment_context.AttachmentContext",
    transport: MailTransport,
    dry_run: bool,
    record,
) -> Outcome:
    """Запрашивает ответ модели и отправляет его пользователю."""
    from src import llm, reply_builder, summarizer

    # История читается до записи текущего письма — иначе вопрос попал бы в контекст дважды.
    # MAX_HISTORY_MESSAGES=0 читает сессию целиком: объём держит свёртка, а обрезка по числу строк отрезала бы историю раньше времени.
    history = (
        storage.get_history(session_id, MAX_HISTORY_MESSAGES or None) if session_id else []
    )

    # Команда /summary первой строкой — ответом служит сама сводка, обычная генерация не выполняется.
    # Письмо с приложенным документом сюда не идёт: команда относится к вложению, а свёртка стёрла бы контекст сессии.
    if session_id and not incoming.attachments and summarizer.is_summary_request(incoming.body):
        return _summarize_on_request(
            incoming, session_id, title, prompt, transport, dry_run, record
        )

    # Описание документов уходит в запрос, но не в таблицу messages — иначе оно копилось бы в каждой строке истории; в базе остаётся только текст человека.
    request_prompt = attachments_ctx.prompt_prefix + prompt

    # Автоматическая свёртка — когда сессия с текущим запросом переросла SESSION_MAX_CHARS; свёртка покрывает реплики до этого письма.
    # Порог считается по request_prompt: блок описаний вложений занимает окно модели наравне с текстом и может вытолкнуть историю сам по себе.
    history, folded = summarizer.fit_session(session_id, history, request_prompt, dry_run)

    if not dry_run:
        # body_raw хранит тело вместе с цитатой — всей прежней перепиской треда, включая людей, не писавших сервису.
        # STORE_RAW_BODY по умолчанию выключен: поле только для разбора промахов эвристики цитат.
        storage.add_message(
            session_id, "user", prompt, incoming.message_id,
            incoming.body_raw if STORE_RAW_BODY else "",
        )

    try:
        # обращение к модели повторяется: запрос идемпотентен, и сетевой сбой
        # на нём лечится сам
        answer = _retry(
            lambda: llm.generate(history, request_prompt, attachments_ctx.files),
            attempts=3,
            what="генерация ответа",
        )

    # ветка недоступной модели: пользователь получает письмо с причиной,
    # заявка закрывается статусом error, и письмо попадает в команду retry
    except Exception as exc:
        log.error("модель не ответила: %s", exc)
        _reply(incoming, title, LLM_ERROR_NOTICE.format(error=exc), transport, dry_run)
        if not dry_run:
            storage.finish_message(incoming.message_id, "error", f"LLM: {exc}")
        return Outcome("error", f"модель недоступна: {exc}", session_id)

    # примечания дописываются к ответу модели: обрезка письма, затем вложения
    if truncated:
        answer += TRUNCATION_NOTICE.format(limit=MAX_PROMPT_CHARS)
    answer += attachments_ctx.notice

    # Предупреждение о свёртке — первой строкой, до метки [Sofi]: свёртка прошла молча, и это единственное место, где пользователь о ней узнаёт.
    if folded:
        answer = f"{SUMMARY_DEGRADATION_NOTICE}\n\n{answer}"

    # Метка [Sofi] ставится до отправки и до записи в messages: по ней модель отличает свою прежнюю реплику от реплики пользователя.
    # build_reply вызывает mark_answer повторно — функция идемпотентна.
    answer = reply_builder.mark_answer(answer)

    try:
        # Отправка — одной попыткой: доставка через Exchange не идемпотентна, повтор после неясного исхода дал бы получателю второе письмо.
        sent_message_id = _reply(incoming, title, answer, transport, dry_run)

    # ветка сбоя отправки: такие сбои проходят сами, поэтому заявка снимается.
    # письмо остаётся непрочитанным и попадает в следующий проход
    except Exception as exc:
        log.error("не удалось отправить ответ: %s", exc)
        if not dry_run:
            storage.release_message(incoming.message_id)
        return Outcome("error", f"отправка: {exc}", session_id)

    # примерка историю не пишет
    if dry_run:
        return Outcome("ok", "ответ сгенерирован, письмо не отправлено (dry-run)")

    # Письмо уже доставлено, дальше — только записи в базу; сбой любой из них оставляет статус ok.
    # Перевод заявки в error отправил бы письмо в retry и дал бы второй ответ на тот же вопрос.
    return _record_sent(incoming, session_id, answer, sent_message_id, record)


# Сворачивает переписку по просьбе пользователя; вызывается под замком сессии.
# После — контекст сессии состоит из одной сводки (граница по последней реплике), письма остаются якорями треда для поиска сессии.
def _summarize_on_request(
    incoming: IncomingEmail,
    session_id: int,
    title: str,
    prompt: str,
    transport: MailTransport,
    dry_run: bool,
    record,
) -> Outcome:
    """Сворачивает переписку по просьбе пользователя и отправляет ему сводку."""
    from src import reply_builder, summarizer

    # Контекст читается заново без ограничения по числу строк — сводка покрывает всю переписку, а history выше урезан MAX_HISTORY_MESSAGES.
    history = summarizer.full_context(session_id)

    # сессия без реплик: сводка состояла бы из одной просьбы её составить
    if not history:
        _reply(incoming, title, SUMMARY_EMPTY_NOTICE, transport, dry_run)
        record("skipped", "сворачивать нечего")
        return Outcome("skipped", "в сессии нет переписки", session_id)

    session = storage.get_session(session_id)
    peer_email = session["peer_email"] if session else incoming.sender

    try:
        # тот же повтор, что у обычной генерации: запрос идемпотентен
        body = _retry(
            lambda: summarizer.summarize(history, title, peer_email),
            attempts=3,
            what="суммаризация переписки",
        )

    # модель недоступна: контекст сессии остаётся прежним, пользователь
    # получает причину и может повторить просьбу
    except Exception as exc:
        log.error("суммаризация не удалась: %s", exc)
        _reply(incoming, title, LLM_ERROR_NOTICE.format(error=exc), transport, dry_run)
        if not dry_run:
            storage.finish_message(incoming.message_id, "error", f"summary: {exc}")
        return Outcome("error", f"суммаризация: {exc}", session_id)

    answer = reply_builder.mark_answer(f"{SUMMARY_DEGRADATION_NOTICE}\n\n{body}")

    if not dry_run:
        # письмо с просьбой пишется репликой: оно якорь треда, а из контекста
        # его уберёт та же сводка
        storage.add_message(
            session_id, "user", prompt, incoming.message_id,
            incoming.body_raw if STORE_RAW_BODY else "",
        )

    try:
        sent_message_id = _reply(incoming, title, answer, transport, dry_run)

    except Exception as exc:
        log.error("не удалось отправить сводку: %s", exc)
        if not dry_run:
            storage.release_message(incoming.message_id)
        return Outcome("error", f"отправка: {exc}", session_id)

    if dry_run:
        return Outcome("ok", "сводка составлена, письмо не отправлено (dry-run)")

    outcome = _record_sent(incoming, session_id, answer, sent_message_id, record)

    # сводка записывается последней: граница берётся по идентификатору
    # последней реплики, а он появляется только после записи ответа
    try:
        storage.add_summary(
            session_id, body, storage.last_message_id(session_id),
            summarizer.context_chars(history), summarizer.REASON_REQUEST,
        )
    # Сводка не записана, но письмо со сводкой у пользователя уже есть — контекст сессии остаётся прежним.
    # Статус ok сохраняется: перевод в error отправил бы письмо в retry и дал бы второй ответ.
    except Exception as exc:
        log.exception("сводка отправлена, но не записана в сессию %s", session_id)
        return Outcome(outcome.status, f"{outcome.detail}; сводка: {exc}", session_id)

    return outcome


# Записывает отправленный ответ в историю сессии и закрывает заявку; выход — Outcome ok, detail называет сбой записи, если был.
# Предусловие: письмо уже отправлено, повторная отправка недопустима.
def _record_sent(
    incoming: IncomingEmail, session_id: int, answer: str, sent_message_id: Optional[str], record
) -> Outcome:
    """Записывает отправленный ответ в историю сессии и закрывает заявку."""
    failures = []

    try:
        storage.add_message(session_id, "assistant", answer, sent_message_id)
    # реплика модели потеряна для истории сессии: следующие письма треда уйдут
    # в модель без этого ответа. письмо получателю при этом доставлено
    except Exception as exc:
        log.exception("ответ отправлен, но не записан в историю сессии %s", session_id)
        failures.append(f"история: {exc}")

    try:
        record("ok", f"сессия {session_id}")
    # Заявка осталась в processing — её подберёт reset_stale_processing, переведёт в error, retry вернёт письмо в очередь.
    # Признак прочитанности уже стоит, второе письмо не уйдёт.
    except Exception as exc:
        log.exception("ответ отправлен, но заявка %s не закрыта", incoming.message_id)
        failures.append(f"журнал: {exc}")

    if failures:
        return Outcome(
            "ok", f"ответ отправлен в сессию {session_id}, сбой записи ({'; '.join(failures)})",
            session_id,
        )
    return Outcome("ok", f"ответ отправлен в сессию {session_id}", session_id)


# Выполняет проход по непрочитанным письмам; выход — RunSummary счётчиков прохода.
# Вызывается командой cli once и циклом run_forever.
def run_once(
    transport: Optional[MailTransport] = None,
    dry_run: bool = False,
    workers: int = WORKERS,
) -> RunSummary:
    """Выполняет один проход по непрочитанным письмам ящика."""
    # свой транспорт закрывается в finally; переданный остаётся открытым
    # у вызывающего кода
    own_transport = transport is None
    transport = transport or get_transport()
    summary = RunSummary()

    try:
        fetched = transport.fetch_unseen()

        # пустой ящик завершает проход без обращения к базе
        if not fetched:
            return summary

        results: List[Tuple[Any, Outcome]] = []

        # Пул потоков включается при нескольких письмах: без него второй сотрудник ждал бы минуты, пока модель отвечает первому.
        if workers > 1 and len(fetched) > 1:
            log.info("писем в проходе: %d, обрабатываю в %d потоках", len(fetched), workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mail") as pool:
                # словарь связывает задачу с дескриптором письма: он понадобится
                # для простановки признака прочитанности
                futures = {
                    pool.submit(process_email, msg, dry_run, transport): handle
                    for handle, msg in fetched
                }

                # as_completed отдаёт задачи по мере готовности
                for future in as_completed(futures):
                    results.append((futures[future], future.result()))
        else:
            for handle, msg in fetched:
                results.append((handle, process_email(msg, dry_run, transport)))

        # Признак прочитанности ставится в главном потоке после пула: сессия EWS не принимает команды из нескольких потоков разом, а однократность ответа держит журнал.
        seen = []
        for handle, outcome in results:
            summary.add(outcome)
            if MARK_SEEN and not dry_run and outcome.can_mark_seen:
                seen.append(handle)

        # Отметка — одним запросом на пачку: после простоя демона проход приносит десятки писем, и запрос на каждое дал бы столько же обращений к Exchange.
        if seen:
            transport.mark_seen_bulk(seen)
    finally:
        if own_transport:
            transport.close()

    return summary


# Опрашивает ящик по интервалу; ставит обработчики SIGINT/SIGTERM и возвращает управление после сигнала остановки.
def run_forever(
    interval: int = POLL_INTERVAL_SEC, dry_run: bool = False, workers: int = WORKERS
) -> None:
    """Опрашивает ящик по интервалу с переподключением при разрывах."""
    running = True

    # обработчик сигнала опускает флаг цикла: текущая итерация доводится
    # до конца, и демон останавливается на границе прохода
    def stop(signum, _frame):
        nonlocal running
        log.info("получен сигнал %s, останавливаюсь", signal.Signals(signum).name)
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    transport = get_transport()

    # backoff растёт при сбоях и возвращается к interval после удачного прохода
    backoff = interval

    log.info(
        "демон запущен: %s, опрос каждые %d с, потоков %d%s, срок хранения %s",
        MAIL_ADDRESS, interval, workers, " [dry-run]" if dry_run else "",
        f"{RETENTION_DAYS} дней" if RETENTION_DAYS > 0 else "бессрочно",
    )

    # Уборка по срокам хранения работает внутри демона, а не внешним cron, — сроки соблюдаются по построению, без внешней настройки.
    # next_purge=0 даёт первый прогон сразу на старте, дальше раз в сутки; частые перезапуски демона дают лишние прогоны.
    next_purge = 0.0

    # Сверка SQLite<->Open WebUI (cli reconcile) в цикл намеренно не включена: расхождения копятся медленно, оператор решает их вручную.

    while running:
        try:
            # Зависшие в processing заявки сбрасываются на каждой итерации: письмо уже помечено прочитанным и без сброса ответа не получит.
            # Запрос идёт по индексу idx_processed_status и стоит одного update.
            _reset_stale()

            # монотонные часы не зависят от перевода системного времени
            if time.monotonic() >= next_purge:
                run_retention()
                next_purge = time.monotonic() + 24 * 3600

            summary = run_once(transport, dry_run=dry_run, workers=workers)

            # строка в лог пишется только по непустому проходу
            if summary.fetched:
                log.info(
                    "обработано %d: ответов %d, пропущено %d, ошибок %d",
                    summary.fetched, summary.answered, summary.skipped, summary.failed,
                )

            # удачный проход возвращает паузу к штатному интервалу
            backoff = interval

        # Сбой цикла (разрыв соединения, таймаут): пауза удваивается до 300с, чтобы не долбить сервер при длительном сбое.
        except Exception as exc:
            backoff = min(backoff * 2, 300)
            log.warning("сбой цикла (%s), переподключение через %d с", exc, backoff)
            try:
                transport.reconnect()
            # неудачное переподключение повторится на следующей итерации
            except Exception:
                log.debug("переподключение не удалось, повтор на следующей итерации", exc_info=True)

        # пауза дробится по секунде: сигнал остановки прерывает ожидание
        # в пределах секунды
        for _ in range(backoff):
            if not running:
                break
            time.sleep(1)

    transport.close()
    log.info("демон остановлен")


# выход: число заявок, переведённых из processing в error.
# Порог — удвоенный таймаут модели: дольше означает остановленный либо застрявший поток.
def _reset_stale() -> int:
    """Переводит зависшие заявки журнала в статус error."""
    stale = storage.reset_stale_processing(LLM_TIMEOUT_SEC * 2)
    if stale:
        log.warning("%d писем зависли в обработке дольше %d с, помечены как error",
                    stale, LLM_TIMEOUT_SEC * 2)
    return stale


# выход: (удалено файлов, удалено сессий, удалено записей журнала).
# Вызывается из цикла демона раз в сутки и командами cli purge/purge-files.
def run_retention() -> Tuple[int, int, int]:
    """Выполняет уборку по обоим срокам хранения за один проход."""
    from src import owui_files

    # первыми уходят файлы, просроченные по собственному сроку хранения
    files, _ = owui_files.purge_expired(ATTACHMENT_RETENTION_DAYS)

    # значение 0 и меньше отключает удаление переписки
    if RETENTION_DAYS <= 0:
        return (files, 0, 0)

    # Файлы просроченных сессий снимаются в Open WebUI до удаления сессий: иначе каскад унёс бы session_files, и файлы остались бы без записей о происхождении.
    files += owui_files.forget(storage.file_ids_of_expired_sessions(RETENTION_DAYS))

    # идентификаторы читаются до DELETE в purge_older_than: после него
    # взять их для чистки _session_locks было бы уже неоткуда
    expiring = storage.session_ids_older_than(RETENTION_DAYS)
    sessions, journal = storage.purge_older_than(RETENTION_DAYS)
    _prune_session_locks(expiring)
    return (files, sessions, journal)


# вход: транспорт (при None — свой, закрывается в finally). выход: число писем, возвращённых в очередь.
# Письма подберёт следующий проход обычным путём — отдельной ветки обработки для них нет.
def retry_failed(transport: Optional[MailTransport] = None) -> int:
    """Возвращает письма со статусом error в очередь обработки."""
    own_transport = transport is None
    transport = transport or get_transport()
    restored = 0

    try:
        for row in storage.list_failed():
            message_id = row["message_id"]

            # нулевой результат означает, что письма в папке уже нет: строка
            # журнала при этом остаётся, повторную обработку запускать не по чему
            if not transport.unsee_by_message_id(message_id):
                log.warning("письмо %s не найдено в папке — пропускаю", message_id)
                continue

            # запись журнала удаляется после успешного снятия признака: порядок
            # исключает письмо, забытое журналом и оставшееся прочитанным
            storage.forget_message(message_id)
            restored += 1
            log.info("возвращено в очередь: %s", message_id)
    finally:
        if own_transport:
            transport.close()

    return restored
