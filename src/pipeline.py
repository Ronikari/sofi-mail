# оркестрация обработки письма: письмо -> сессия -> ответ модели -> письмо.
# порядок одного письма: разбор -> отсев автоматических и посторонних
# отправителей -> заявка в журнале -> поиск либо создание сессии -> загрузка
# вложений в Open WebUI -> под замком сессии: сборка запроса, обращение
# к модели, отправка ответа, запись реплик в базу.
# порядок демона: цикл опроса ящика с пулом потоков, сброс зависших заявок
# на каждой итерации, уборка по срокам хранения раз в сутки.
# вход: MIME-сообщения от transport.fetch_unseen и от команды cli ingest-eml.
# выход: Outcome на письмо и RunSummary на проход.
# разбор письма выполняет email_parser.py, вложения — attachment_context.py,
# запрос к модели — llm.py, свёртку переписки — summarizer.py, хранение —
# storage.py, отправку — transport.py, маскирование для лога — redact.py.
# вызывается из cli.py командами serve, once, retry, ingest-eml.
#
# команды once и serve обращаются к одной функции process_email, поэтому
# поведение при отладке совпадает с поведением демона

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

# замок вокруг поиска и создания сессии. операция «найти либо создать» здесь
# неделима: два письма с одинаковой темой, пришедшие в одном проходе, оба
# не нашли бы сессию и создали каждое свою.
# замок процессный, демон рассчитан на один процесс; запуск нескольких
# экземпляров на одну базу потребовал бы транзакции в storage.py
_session_create_lock = threading.Lock()

# замки на сессию: реплики одной сессии обрабатываются по очереди, разные
# сессии — параллельно. два письма одного треда без замка прочитали бы одну
# и ту же историю, и каждый ответ собрался бы без учёта второго вопроса.
# под замком идут только чтение истории, обращение к модели, отправка и записи
# в базу; загрузка вложений в Open WebUI занимает минуты и выполняется до него
_session_locks_guard = threading.Lock()
_session_locks: Dict[int, threading.Lock] = {}


# вход: идентификатор сессии; значение 0 обозначает примерку без сессии.
# выход: замок сессии, общий для всех потоков.
# setdefault под _session_locks_guard создаёт замок один раз: без охраны два
# потока получили бы два разных объекта на одну сессию
def _session_lock(session_id: int) -> threading.Lock:
    """Отдаёт замок, сериализующий обработку писем одной сессии."""
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


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
# пересылка без единого слова от себя. тело такого письма состоит из чужого
# треда без указания авторства реплик: ответ по нему опирался бы на переписку
# людей, которые сервису не писали, и сам тред лёг бы в историю сессии
# репликой пересылающего
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
# предупреждение о состоявшейся свёртке. ставится первой строкой ответа
# и в письме со сводкой по просьбе пользователя, и в обычном ответе, перед
# которым сессия свернулась по достижении SESSION_MAX_CHARS.
# формулировка объясняет то, чего не видно в почтовом клиенте: письма остаются
# в цепочке, но в контекст сессии больше не входят, и ответы дальше опираются
# на сводку — часть подробностей переписки модели уже недоступна
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

    # выход: True для писем, которые допустимо пометить прочитанными.
    # письмо со статусом error остаётся непрочитанным: команда retry находит
    # его в папке по Message-ID
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


# вход: action — вызываемый объект без аргументов; attempts — число попыток;
# what — название операции для лога.
# выход: результат первой успешной попытки.
# исключение последней попытки поднимается наружу.
# побочный эффект: паузы между попытками, суммарно до 2+4 секунд при attempts=3.
# повтор применим к операциям, безопасным при частичном выполнении: обращение
# к модели повторить можно, отправку письма — нет, доставка через Exchange
# не идемпотентна
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


# вход: разобранное письмо, название сессии, текст ответа, транспорт и режим
# примерки.
# выход: Message-ID отправленного письма; None в режиме dry_run.
# побочный эффект: отправка письма через Exchange либо печать в консоль.
# вызывается один раз на письмо: повтор при неясном исходе отправки дал бы
# получателю второе письмо
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

    # заголовки треда и разговора берутся из входящего письма: ответ продолжает
    # тот же тред и тот же разговор Exchange.
    # sender_name, quoted_body и sent_date дают reply_builder цитату входящего
    # письма — без неё ответ доходит без блока «Reply», по которому Outlook
    # показывает, на какое именно письмо пользователя отвечает модель.
    # quoted_body несёт incoming.body: новый текст этого письма без цепочки
    # прежних цитат. incoming.body_raw протащил бы в цитату всю историю сессии,
    # и её объём рос бы с каждым ответом
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
        quoted_body=incoming.body,
        sent_date=incoming.date,
    )


# вход: разобранное письмо.
# выход: идентификатор существующей сессии либо None.
# оба поиска ограничены адресом отправителя: сессия описывает переписку
# с одним человеком, и письмо с другого адреса её не продолжает
def _find_session(incoming: IncomingEmail) -> Optional[int]:
    """Ищет сессию письма по заголовкам треда, затем по теме."""
    # заголовки треда проверяются первыми: тема повторяется у разных писем,
    # Message-ID уникален.
    # собственный идентификатор письма стоит перед предками: письмо разбирается
    # второй раз после сбоя отправки (storage.release_message), и его реплика
    # уже лежит в своей сессии. без этой проверки повтор открыл бы новую сессию,
    # вопрос потерялся бы на ограничении UNIQUE колонки messages.message_id,
    # и ответ лёг бы в пустую сессию отдельно от вопроса
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


# вход: исходное MIME-сообщение и его разбор.
# выход: текст причины отказа; None для письма, подлежащего обработке.
# отправитель об этом отказе не уведомляется
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


# вход: MIME-сообщение письма, режим примерки и транспорт; при transport=None
# соединение открывается вызовом transport.get_transport.
# выход: Outcome со статусом ok, skipped либо error; исключения наружу
# не выпускаются.
# побочные эффекты: записи в базу, загрузка файлов, запрос к модели, отправка
# письма.
# вызывается из нескольких потоков одновременно, поэтому состояние выносится
# в sqlite: заявка в журнале атомарна, порядок реплик внутри сессии держат замки
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

    # заявка ставится до обращения к модели: перезапуск демона в середине
    # обработки не приводит ко второму ответу на то же письмо. она же отсекает
    # повторную обработку письма, дважды попавшего в один проход.
    # в режиме примерки заявка не ставится: она заняла бы письмо, и обычный
    # запуск его пропустил бы
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


# вход: разобранное письмо и режим примерки.
# выход: идентификатор сессии; 0 обозначает примерку нового треда, где сессия
# не создаётся.
# побочный эффект: строка в таблице sessions.
# поиск и создание идут под одним замком: без него два письма с одинаковой темой
# из одного прохода создали бы две сессии
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


# вход: разобранное письмо, транспорт и режим примерки.
# выход: Outcome с итогом обработки.
# предусловие: письмо застолблено в журнале вызовом storage.claim_message.
# побочные эффекты: записи в базу, загрузка файлов, отправка письма.
# в режиме примерки состояние не меняется: прогон повторяется с тем же
# результатом
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

    # обрезка длинного письма; о ней пользователю сообщает TRUNCATION_NOTICE.
    # значение MAX_PROMPT_CHARS=0 обрезку отключает: письмо целиком входит
    # в запрос, и это единственный предел на его размер — свёртка в summarizer.py
    # покрывает историю сессии, но не текст самого письма; сверх этого предела
    # объём запроса не режется нигде (llm.warn_over_budget только логирует)
    truncated = MAX_PROMPT_CHARS > 0 and len(prompt) > MAX_PROMPT_CHARS
    if truncated:
        log.warning("письмо длиной %d символов обрезано до %d", len(prompt), MAX_PROMPT_CHARS)
        prompt = prompt[:MAX_PROMPT_CHARS]

    session_id = _resolve_session(incoming, dry_run)
    session = storage.get_session(session_id) if session_id else None

    # название существующей сессии сохраняется: письмо продолжает её тред
    # под прежним заголовком
    title = session["title"] if session else incoming.title

    # разбор и загрузка вложений идут до замка сессии: пять файлов по таймауту
    # ATTACHMENT_PROCESS_TIMEOUT_SEC занимают минуты, и письма того же треда
    # без документов ждали бы всё это время
    upload = attachment_context.upload_attachments(incoming, dry_run)

    try:
        # замок сессии сериализует реплики одного треда; разные сессии
        # обрабатываются параллельно
        with _session_lock(session_id):
            # запись файлов в базу и чтение файлов прежних писем идут под тем же
            # замком: письма одного треда, пришедшие разом, иначе записали бы
            # свои файлы вперемешку, и второе увидело бы в истории документ,
            # про который его ещё не спрашивали
            attachments_ctx = attachment_context.build_context(
                incoming, session_id, upload, dry_run
            )

            # вложение отвергнуто проверкой либо сервисом документов, и до
            # модели не дошло ни одного файла. ответ по одному тексту письма
            # был бы ответом не на заданный вопрос: вопрос задан по документу,
            # которого модель не видит. письмо закрывается причиной отказа,
            # запроса к модели не происходит.
            # ветка стоит до проверки пустого тела: она срабатывает и на письме
            # с текстом, где EMPTY_BODY_NOTICE неприменим
            if upload.rejected and not attachments_ctx.files:
                notice = attachments_ctx.standalone_notice
                _reply(incoming, title, notice, transport, dry_run)
                record("skipped", "вложение отклонено проверкой: " + "; ".join(upload.rejected))
                return Outcome("skipped", "вложение отклонено", session_id)

            # письмо из одних вложений, ни одно из которых до модели не дошло.
            # запрос без вопроса и без документа дал бы ответ по пустому месту,
            # а причина отказа выглядела бы оговоркой к этому ответу
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
        # уборка идёт в finally: файл примерки не переживает прогон ни на одной
        # ветке выхода, включая исключение. записей о нём в базе нет, и удалить
        # его позже будет нечем.
        # список берётся из результата загрузки: он заполнен до входа в замок,
        # и сбой внутри build_context уборку не отменяет
        if dry_run:
            attachment_context.drop_uploaded(upload.file_ids)


# вход: письмо, сессия, название, текст вопроса, признак обрезки, контекст
# вложений, транспорт, режим примерки и функция record для закрытия заявки.
# выход: Outcome с итогом.
# побочные эффекты: запрос к модели, отправка письма, записи реплик в базу.
# предусловие: вызывается под замком сессии
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

    # история читается до записи текущего письма: иначе вопрос попал бы
    # в контекст дважды.
    # значение MAX_HISTORY_MESSAGES=0 читает сессию целиком: объём контекста
    # держит свёртка в summarizer.py, и обрезка по числу строк отрезала бы
    # историю раньше, чем это станет нужно
    history = (
        storage.get_history(session_id, MAX_HISTORY_MESSAGES or None) if session_id else []
    )

    # команда /summary первой строкой: ответом на такое письмо служит сама
    # сводка, и обычная генерация для него не выполняется.
    # письмо с приложенным документом по этой ветке не идёт: команда рядом
    # с вложением относится к этому вложению, а свёртка стёрла бы контекст
    # сессии. документ, приложенный к прежним письмам сессии, ветку
    # не закрывает: свернуть переписку по документу — обычная просьба
    if session_id and not incoming.attachments and summarizer.is_summary_request(incoming.body):
        return _summarize_on_request(
            incoming, session_id, title, prompt, transport, dry_run, record
        )

    # описание документов уходит в запрос и в таблицу messages не попадает:
    # в базе остаётся текст, написанный человеком, а описание пересобирается
    # из session_files на каждом письме треда. запись описания в реплику
    # копила бы его в каждой строке истории
    request_prompt = attachments_ctx.prompt_prefix + prompt

    # автоматическая свёртка: сессия вместе с текущим запросом переросла
    # SESSION_MAX_CHARS. свёртка покрывает реплики до текущего письма, ответ
    # на него собирается уже по сводке.
    # порог считается по request_prompt: блок описаний документов (до
    # MAX_ATTACHMENT_CONTEXT_CHARS) занимает часть окна модели наравне
    # с текстом письма. письмо с коротким собственным текстом и объёмным
    # блоком вложений всё равно выталкивает историю сессии за MAX_CONTEXT_CHARS
    # на шаге llm.build_messages — тот же класс регресса, что уже чинили
    # раньше для тела письма (см. review.md, п.6)
    history, folded = summarizer.fit_session(session_id, history, request_prompt, dry_run)

    if not dry_run:
        # body_raw хранит тело вместе с цитатой, а в цитате едет вся прежняя
        # переписка треда, включая реплики людей, не писавших сервису.
        # флаг STORE_RAW_BODY по умолчанию выключен: поле служит разбору
        # промахов эвристики цитат
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

    # предупреждение о свёртке идёт первой строкой, до метки [Sofi]: свёртка
    # прошла молча, по достижении предела, и о потере подробностей переписки
    # пользователь узнаёт только отсюда
    if folded:
        answer = f"{SUMMARY_DEGRADATION_NOTICE}\n\n{answer}"

    # метка [Sofi] ставится здесь, до отправки и до записи в таблицу messages:
    # в истории сессии реплика модели помечена тем же признаком, что и в письме,
    # и модель отличает свою прежнюю реплику от реплики пользователя.
    # build_reply вызывает mark_answer повторно, функция идемпотентна
    answer = reply_builder.mark_answer(answer)

    try:
        # отправка выполняется одной попыткой: доставка через Exchange
        # не идемпотентна, и повтор после неясного исхода даёт получателю
        # второе письмо с тем же ответом
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

    # письмо получателю доставлено, и дальше идут только записи в базу.
    # сбой любой из них оставляет статус ok: перевод заявки в error отправил бы
    # письмо в команду retry, та сняла бы признак прочитанности, и следующий
    # проход сгенерировал бы второй ответ на тот же вопрос
    return _record_sent(incoming, session_id, answer, sent_message_id, record)


# вход: письмо, сессия, её название, текст письма, транспорт, режим примерки
# и функция record.
# выход: Outcome с итогом.
# побочные эффекты: запрос к модели, отправка письма, реплики и сводка в базе.
# предусловие: вызывается под замком сессии.
#
# после этой ветки контекст сессии состоит из одной сводки: граница свёртки
# ставится по последней записанной реплике, то есть покрывает и само письмо
# с просьбой, и ответ на него. письма остаются якорями треда в таблице
# messages — по ним следующее письмо пользователя находит свою сессию
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

    # контекст читается заново и без ограничения по числу строк: сводка
    # покрывает всю переписку до этого письма, а история выше прочитана
    # с MAX_HISTORY_MESSAGES и содержит только последние реплики
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
    # сводка не записана: контекст сессии остался прежним, письмо со сводкой
    # у пользователя уже есть. статус ok сохраняется — перевод заявки в error
    # отправил бы письмо в команду retry и дал бы второй ответ
    except Exception as exc:
        log.exception("сводка отправлена, но не записана в сессию %s", session_id)
        return Outcome(outcome.status, f"{outcome.detail}; сводка: {exc}", session_id)

    return outcome


# вход: письмо, сессия, текст отправленного ответа, его Message-ID и функция
# закрытия заявки.
# выход: Outcome со статусом ok; текст detail называет сбой записи, если он был.
# побочные эффекты: строка в таблице messages и закрытие заявки в processed.
# предусловие: письмо получателю уже отправлено, повторная отправка недопустима
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
    # заявка осталась в статусе processing: её подберёт reset_stale_processing
    # и переведёт в error, а команда retry вернёт письмо в очередь.
    # признак прочитанности при этом уже стоит, и второе письмо не уйдёт
    except Exception as exc:
        log.exception("ответ отправлен, но заявка %s не закрыта", incoming.message_id)
        failures.append(f"журнал: {exc}")

    if failures:
        return Outcome(
            "ok", f"ответ отправлен в сессию {session_id}, сбой записи ({'; '.join(failures)})",
            session_id,
        )
    return Outcome("ok", f"ответ отправлен в сессию {session_id}", session_id)


# вход: транспорт (при None открывается свой), режим примерки и число потоков.
# выход: RunSummary со счётчиками прохода.
# побочные эффекты: обработка писем и простановка признака прочитанности.
# вызывается командой cli once и циклом run_forever
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

        # пул потоков включается на нескольких письмах: время прохода занимает
        # генерация ответа, и при одном потоке второй сотрудник ждёт минуты,
        # пока модель отвечает первому
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

        # признак прочитанности ставится в главном потоке после работы пула:
        # сессия EWS команды из нескольких потоков одновременно не принимает,
        # а однократность ответа держит журнал в таблице processed
        seen = []
        for handle, outcome in results:
            summary.add(outcome)
            if MARK_SEEN and not dry_run and outcome.can_mark_seen:
                seen.append(handle)

        # отметка идёт одним запросом на всю пачку: после простоя демона проход
        # приносит десятки писем, и запрос на каждое дал бы столько же
        # последовательных обращений к Exchange перед следующим опросом
        if seen:
            transport.mark_seen_bulk(seen)
    finally:
        if own_transport:
            transport.close()

    return summary


# вход: интервал опроса в секундах, режим примерки и число потоков.
# побочные эффекты: обработка писем, уборка по срокам хранения, установка
# обработчиков сигналов SIGINT и SIGTERM.
# функция возвращает управление после получения сигнала остановки
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

    # уборка по срокам хранения работает внутри демона: вынесенная во внешний
    # cron, она действует там, где её настроили, а сроки хранения соблюдаются
    # по построению.
    # нулевое значение даёт первый прогон сразу на старте, дальше раз в сутки.
    # отметка живёт в памяти процесса, поэтому частые перезапуски демона дают
    # лишние прогоны уборки
    next_purge = 0.0

    # известное ограничение (architecture-review): сверка SQLite<->Open WebUI
    # (`python -m src.cli reconcile`) в этот цикл намеренно не включена —
    # это осознанное решение. расхождение копится медленно (сбой между
    # загрузкой файла и записью строки — событие редкое), а найденное
    # расхождение решает оператор вручную: --delete-orphans спрашивает
    # подтверждение перед удалением. сверка остаётся ручной командой

    while running:
        try:
            # заявки, зависшие в статусе processing, сбрасываются на каждой
            # итерации: поток, застрявший на сетевом вызове, оставляет заявку
            # висеть, а само письмо помечается прочитанным на этом же проходе
            # и без сброса ответа уже не получит.
            # запрос идёт по индексу idx_processed_status и стоит одного update
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

        # ветка сбоя цикла: разрыв соединения, отказ сети, таймаут.
        # пауза удваивается до 300 секунд, чтобы частота запросов к серверу
        # при длительном сбое оставалась низкой
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
# порогом служит удвоенный таймаут модели: обработка письма дольше него
# означает остановленный либо застрявший поток
def _reset_stale() -> int:
    """Переводит зависшие заявки журнала в статус error."""
    stale = storage.reset_stale_processing(LLM_TIMEOUT_SEC * 2)
    if stale:
        log.warning("%d писем зависли в обработке дольше %d с, помечены как error",
                    stale, LLM_TIMEOUT_SEC * 2)
    return stale


# выход: тройка (удалено файлов, удалено сессий, удалено записей журнала).
# побочные эффекты: удаление файлов в Open WebUI и строк в базе.
# вызывается из цикла демона раз в сутки и командами cli purge, purge-files
def run_retention() -> Tuple[int, int, int]:
    """Выполняет уборку по обоим срокам хранения за один проход."""
    from src import owui_files

    # первыми уходят файлы, просроченные по собственному сроку хранения
    files, _ = owui_files.purge_expired(ATTACHMENT_RETENTION_DAYS)

    # значение 0 и меньше отключает удаление переписки
    if RETENTION_DAYS <= 0:
        return (files, 0, 0)

    # файлы просроченных сессий снимаются в Open WebUI до удаления самих
    # сессий: каскад унёс бы строки session_files вместе с сессией, и файлы
    # остались бы в общем хранилище без записей о своём происхождении
    files += owui_files.forget(storage.file_ids_of_expired_sessions(RETENTION_DAYS))

    sessions, journal = storage.purge_older_than(RETENTION_DAYS)
    return (files, sessions, journal)


# вход: транспорт; при None открывается свой и закрывается в finally.
# выход: число писем, возвращённых в очередь.
# побочные эффекты: снятие признака прочитанности в ящике и удаление строк
# журнала.
# письма подберёт следующий проход обычным путём, отдельной ветки обработки
# для них нет
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
