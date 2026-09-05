"""Единый пайплайн: письмо -> сессия -> ответ модели -> письмо.

Вся оркестрация живёт здесь; остальные модули — библиотеки без самостоятельного
запуска. `once` и `serve` вызывают один и тот же `process_email`, поэтому
поведение при отладке и в демоне совпадает по построению.
"""

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from email.message import Message
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from src import redact, storage
from src.config import (
    ATTACHMENT_FULL_CONTEXT_CHARS,
    ATTACHMENT_FULL_CONTEXT_PAGES,
    ATTACHMENT_MAX_CHARS,
    ATTACHMENT_MAX_COUNT,
    ATTACHMENT_MAX_SESSION_FILES,
    ATTACHMENT_RETENTION_DAYS,
    ATTACHMENTS_ENABLED,
    LLM_TIMEOUT_SEC,
    LLM_WEB_URL,
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

# Создание сессии: найти-или-создать должно быть неделимым, иначе два письма
# с одной темой, пришедшие в одном проходе, создадут две сессии вместо одной.
# Блокировка процессная — демон рассчитан на один процесс; для запуска
# нескольких экземпляров на одну базу понадобилась бы транзакция в storage.
_session_create_lock = threading.Lock()

# Реплики одной сессии обрабатываются по очереди, разные сессии — параллельно.
# Иначе два письма одного треда прочитают одну и ту же историю, и каждый ответ
# будет построен без учёта второго вопроса.
_session_locks_guard = threading.Lock()
_session_locks: Dict[int, threading.Lock] = {}


def _session_lock(session_id: int) -> threading.Lock:
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


EMPTY_BODY_NOTICE = (
    "В письме не нашлось текста вопроса — возможно, он целиком состоял из цитаты "
    "или вложения. Напишите вопрос обычным текстом в теле письма."
)
TNEF_NOTICE = (
    "Письмо пришло в формате RTF (текст упакован в вложение winmail.dat), поэтому "
    "прочитать вопрос не удалось. Переключите формат письма на «Обычный текст» "
    "или HTML в настройках Outlook и отправьте вопрос заново."
)
# Письмо без текста, но с документом: вопрос за пользователя формулируем сами
DOCUMENT_ONLY_PROMPT = (
    "Письмо пришло без текста, только с приложенным документом. Коротко изложи, "
    "о чём документ и что в нём главное."
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
# Про вложения пользователю говорим всегда, когда с ними что-то не так или
# когда документ подан не целиком: иначе ответ по половине документа выглядит
# как ответ по всему, и проверить это по письму нельзя.
#
# Тексты здесь хранятся без обёртки, а обёртку даёт ATTACHMENT_NOTE. Причина
# в том, что у этих же слов есть вторая работа: когда до модели не доехало
# ничего и ответа не будет вовсе, они уходят пользователю отдельным письмом,
# где приписка в скобках к несуществующему ответу выглядела бы нелепо.
ATTACHMENT_NOTE = "\n\n(Примечание: {text})"
ATTACHMENT_SKIPPED_TEXT = "не удалось приложить к вопросу — {details}."
ATTACHMENT_FOCUSED_TEXT = (
    "{details} — отвечаю по релевантным фрагментам и оглавлению, "
    "а не по всему тексту. Уточняющий вопрос про конкретный раздел даст более точный ответ."
)
# Документ, который не поместился целиком и остался без оглавления, — случай
# не из ряда «файл не приняли»: файл в порядке, и вопрос по нему у человека
# никуда не делся. Поэтому вместе с причиной уходят оба пути, которыми ответ
# всё-таки получится, — иначе следующим письмом приедет тот же файл
ATTACHMENT_TOO_LARGE_TEXT = (
    "{details} — обработать почтой не получилось. Документ не помещается "
    "в запрос целиком, а оглавления, по которому нашёлся бы нужный раздел, в нём нет: "
    "ответ собрался бы из случайных фрагментов, но выглядел бы как ответ по всему "
    "документу.{ways}"
)
# Вторая причина того же отказа: документ велик настолько, что сервис не берёт
# его и поиском. Текст свой, потому что первый объяснял бы человеку не то:
# оглавление здесь ни при чём, и, добавив его, он ничего не изменит
ATTACHMENT_TOO_LONG_TEXT = (
    "{details} — обработать почтой не получилось: сервис берёт документы "
    "до {limit} знаков, а этот больше. Даже поиск по такому документу "
    "не дал бы ответа, за который можно ручаться.{ways}"
)


@dataclass
class Outcome:
    """Итог обработки одного письма."""

    status: str  # ok | skipped | error
    detail: str = ""
    session_id: Optional[int] = None

    @property
    def can_mark_seen(self) -> bool:
        """Ошибку не «прочитываем»: письмо должно остаться видимым для retry."""
        return self.status in ("ok", "skipped")


@dataclass
class RunSummary:
    fetched: int = 0
    answered: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, outcome: Outcome) -> None:
        self.fetched += 1
        if outcome.status == "ok":
            self.answered += 1
        elif outcome.status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1


def _retry(action: Callable[[], T], attempts: int, what: str) -> T:
    """Повтор с экспоненциальной паузой. Последняя ошибка пробрасывается."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("%s: попытка %d/%d не удалась (%s), повтор через %.0f с", what, attempt, attempts, exc, delay)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("недостижимо")


def _reply(
    incoming: IncomingEmail,
    title: str,
    body: str,
    transport: MailTransport,
    dry_run: bool,
) -> Optional[str]:
    """Отправка письма пользователю (или печать в консоль при dry-run)."""
    if dry_run:
        print(f"\n--- [dry-run] ответ для {incoming.sender} (сессия «{title}») ---\n{body}\n---\n")
        return None
    return transport.send_reply(
        to_address=incoming.sender,
        subject=incoming.subject,
        body=body,
        session_title=title,
        in_reply_to=incoming.message_id,
        references=incoming.references,
    )


def _find_session(incoming: IncomingEmail) -> Optional[int]:
    """Существующая сессия письма или None.

    Порядок важен: заголовки треда надёжнее темы, потому что тема повторяется
    у разных писем, а Message-ID уникален.

    Собственный Message-ID письма идёт первым — раньше предков. Письмо
    разбирается второй раз после сбоя отправки (см. `release_message`), и его
    реплика уже лежит в своей сессии: без этой проверки повтор открывал бы новую
    сессию, вопрос молча терялся бы на UNIQUE в messages.message_id, а ответ
    ложился бы в пустую сессию отдельно от вопроса.

    Оба поиска ограничены адресом отправителя: сессия — это переписка с одним
    человеком, и письмо с чужого адреса не должно продолжать её ни по теме,
    ни по заголовкам треда.
    """
    session_id = storage.find_session_by_message_ids(
        [incoming.message_id, *incoming.ancestor_ids], incoming.sender
    )
    if session_id:
        log.debug("сессия %s найдена по заголовкам треда", session_id)
        return session_id

    if THREAD_BY_SUBJECT:
        session_id = storage.find_session_by_subject(incoming.sender, incoming.title)
        if session_id:
            log.debug("сессия %s найдена по теме %s", session_id, redact.subject(incoming.title))
            return session_id

    return None


def _rejection_reason(msg: Message, incoming: IncomingEmail) -> Optional[str]:
    """Причина не отвечать на письмо вовсе (без уведомления отправителя)."""
    automated = automated_reason(msg, MAIL_ADDRESS)
    if automated:
        return automated
    if not is_sender_allowed(incoming.sender):
        # молча: ответ подтвердил бы спамеру, что ящик живой
        return f"отправитель {redact.email_addr(incoming.sender)} не в ALLOWED_SENDERS"
    return None


def process_email(
    msg: Message, dry_run: bool = False, transport: Optional[MailTransport] = None
) -> Outcome:
    """Обработать одно письмо. Исключения наружу не выпускаются.

    Вызывается из нескольких потоков сразу, поэтому состояние наружу выносится
    только в SQLite: заявка в журнале атомарна, а порядок реплик внутри сессии
    держат блокировки ниже.
    """
    transport = transport or get_transport()
    incoming = parse_email(msg)
    log.info(
        "письмо от %s: %s", redact.email_addr(incoming.sender), redact.subject(incoming.subject)
    )

    reason = _rejection_reason(msg, incoming)
    if reason:
        log.info("пропущено: %s", reason)
        return Outcome("skipped", reason)

    # заявка на письмо ДО генерации: перезапуск демона в середине обработки
    # не должен приводить ко второму ответу на то же письмо. Она же исключает
    # двойную обработку, если одно письмо попало в проход дважды.
    # В dry-run не заявляем ничего: иначе «примерка» съела бы письмо, и обычный
    # запуск его уже не обработал бы
    if not dry_run and not storage.claim_message(incoming.message_id):
        log.debug("письмо %s уже обработано", incoming.message_id)
        return Outcome("skipped", "уже в журнале обработки")

    try:
        return _process_claimed(incoming, transport, dry_run)
    except Exception as exc:  # не даём одному письму уронить весь цикл
        log.exception("необработанная ошибка на письме %s", incoming.message_id)
        if not dry_run:
            storage.finish_message(incoming.message_id, "error", str(exc))
        return Outcome("error", str(exc))


def _resolve_session(incoming: IncomingEmail, dry_run: bool) -> int:
    """Сессия письма; 0 — примерка нового треда, сессию не создаём.

    Поиск и создание — под одной блокировкой: без неё два письма с одинаковой
    темой, пришедшие в одном проходе, оба не нашли бы сессию и создали каждый
    свою.
    """
    with _session_create_lock:
        existing = _find_session(incoming)
        if existing:
            return existing
        if dry_run:
            return 0
        session_id = storage.create_session(incoming.title, incoming.sender)
        log.info(
            "новая сессия %s: %s с %s",
            session_id, redact.subject(incoming.title), redact.email_addr(incoming.sender),
        )
        return session_id


@dataclass
class AttachmentContext:
    """Что вложения добавляют к запросу и к ответу пользователю."""

    files: List[Dict[str, Any]] = field(default_factory=list)  # ссылки для запроса
    prompt_prefix: str = ""  # описание документов перед вопросом
    notes: List[str] = field(default_factory=list)  # что сказать про вложения
    uploaded: List[str] = field(default_factory=list)  # id, загруженные этим письмом

    @property
    def notice(self) -> str:
        """Примечания припиской к ответу модели."""
        return "".join(ATTACHMENT_NOTE.format(text=text) for text in self.notes)

    @property
    def standalone_notice(self) -> str:
        """Те же примечания как самостоятельное письмо — ответа не будет."""
        return "\n\n".join(self.notes)


def _describe_stored(rows: Sequence) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Файлы прежних писем треда: ссылки и описания для запроса.

    Документ, присланный в первом письме, обсуждают и во втором, и в пятом —
    поэтому ссылки на живые файлы сессии уходят в каждый запрос. Оглавление
    берётся из базы: пересобирать его было бы не из чего, текст документа
    у нас не хранится.
    """
    from src import owui_files

    links, notes = [], []
    for row in rows:
        full = bool(row["full_context"])
        links.append(owui_files.reference(row["file_id"], full))
        size = _size_words(row["pages"], chars=row["chars"]) if row["pages"] else "объём неизвестен"
        if full:
            notes.append(f"Ранее в переписке приложен документ «{row['filename']}» ({size}).")
        else:
            outline = row["outline"] or ""
            head = (
                f"Ранее в переписке приложен документ «{row['filename']}» ({size}), "
                "доступен поиском по содержимому."
            )
            notes.append(f"{head} Структура документа:\n{outline}" if outline else head)
    return links, notes


def _size_words(pages: int, pages_estimated: bool = False, chars: int = 0) -> str:
    """Объём документа для письма человеку: страницы, а знаки — по надобности.

    Тот же выбор, что и в `ParsedDocument.size_label`, но по голым числам:
    сюда они приходят либо из исключения, либо из строки в базе — документа
    рядом в обоих случаях уже нет. Знаки называются только у документа,
    который велик именно ими, — иначе из письма непонятно, почему отказ
    пришёл на пять страниц. У файлов, попавших в базу до появления колонки
    `chars`, объём нулевой, и назван будет только счёт страниц.
    """
    words = f"{pages} стр.{' примерно' if pages_estimated else ''}"
    return f"{words}, {chars} знаков" if chars > ATTACHMENT_FULL_CONTEXT_CHARS else words


def _too_large_notes(documents: Sequence["DocumentTooLargeError"]) -> List[str]:
    """Примечания про документы, по которым ответить почтой нельзя.

    Причин две, и письма по ним разные, поэтому примечаний тоже может быть
    два: с оглавлением человек ничего поделать не может, а прислать документ
    частями — может, и путь этот одинаков в обоих случаях.

    Второй путь — веб-интерфейс — появляется только вместе с адресом: ссылка
    «спросите там» без «там» бесполезнее молчания, а LLM_WEB_URL выводится
    из LLM_BASE_URL и в принципе может оказаться пустым.
    """
    from src.attachments import REASON_TOO_LONG

    ways = (
        " Что можно сделать: прислать отдельным письмом нужную часть документа "
        f"(до {ATTACHMENT_FULL_CONTEXT_PAGES} стр. и {ATTACHMENT_FULL_CONTEXT_CHARS} знаков) "
        "— по ней отвечу целиком"
    )
    ways += (
        f"; либо задать вопрос в веб-интерфейсе {LLM_WEB_URL} — там поиск идёт "
        "по документу целиком."
    ) if LLM_WEB_URL else "."

    notes = []
    for reason, template in (
        (REASON_TOO_LONG, ATTACHMENT_TOO_LONG_TEXT),
        (None, ATTACHMENT_TOO_LARGE_TEXT),  # остальные — «нет оглавления»
    ):
        group = [exc for exc in documents
                 if (exc.reason == reason if reason else exc.reason != REASON_TOO_LONG)]
        if not group:
            continue
        details = "; ".join(
            f"«{exc.filename}» ({_size_words(exc.pages, exc.pages_estimated, exc.chars)})"
            for exc in group
        )
        notes.append(template.format(details=details, ways=ways, limit=ATTACHMENT_MAX_CHARS))
    return notes


def _upload_attachment(attachment, session_id: int, message_id: str, dry_run: bool):
    """Разобрать вложение и положить его текст в Open WebUI.

    Возвращает (ссылка, описание, file_id) либо поднимает AttachmentError
    с причиной, которую можно показать пользователю.
    """
    from src import attachments, owui_files

    document = attachments.parse(attachment)
    file_id = owui_files.upload(attachments.safe_name(document.upload_name), document.text)
    try:
        owui_files.wait_processed(file_id)
    except Exception:
        # недообработанный файл в хранилище — мусор: он не отвечает на вопросы,
        # но занимает место и попадёт под чужие глаза наравне с рабочими
        owui_files.delete(file_id)
        raise

    if not dry_run:
        storage.add_session_file(
            session_id, file_id, document.filename, document.pages,
            document.full_context, document.outline_text(), message_id,
            document.chars,
        )
    return owui_files.reference(file_id, document.full_context), attachments.describe(document), file_id, document


def _prepare_attachments(
    incoming: IncomingEmail, session_id: int, dry_run: bool
) -> AttachmentContext:
    """Вложения письма и прежние файлы треда -> части будущего запроса.

    Ошибка на одном файле не отменяет ответ: пользователь получит ответ по
    остальному письму и примечание о том, что именно не удалось приложить.
    Молча пропустить нельзя — человек ждёт ответа по документу и не увидит,
    что документа модель не получила.
    """
    from src.attachments import AttachmentError, DocumentTooLargeError

    context = AttachmentContext()
    # прежние файлы треда снимаются ДО загрузки новых: иначе только что
    # загруженный файл попал бы в запрос дважды — ссылкой и «ранее приложенным»
    stored = storage.get_session_files(session_id, ATTACHMENT_MAX_SESSION_FILES) if session_id else []
    # то, что уже уехало с этим письмом на прошлой попытке: повторный разбор
    # после сбоя отправки не должен плодить копии в чужом хранилище
    already = storage.filenames_for_message(incoming.message_id) if not dry_run else set()
    descriptions: List[str] = []

    if not ATTACHMENTS_ENABLED:
        if incoming.attachments:
            context.notes.append(
                ATTACHMENT_SKIPPED_TEXT.format(
                    details="работа с вложениями отключена администратором"
                )
            )
        return context

    incoming_files = incoming.attachments[:ATTACHMENT_MAX_COUNT]
    skipped = [
        f"«{a.filename}»: за раз обрабатывается не больше {ATTACHMENT_MAX_COUNT} файлов"
        for a in incoming.attachments[ATTACHMENT_MAX_COUNT:]
    ]
    focused, too_large = [], []
    for attachment in incoming_files:
        if attachment.filename in already:
            log.info(
                "вложение %s уже загружено этим письмом — беру прежний файл",
                attachment.filename,
            )
            continue
        try:
            link, description, file_id, document = _upload_attachment(
                attachment, session_id, incoming.message_id, dry_run
            )
        except DocumentTooLargeError as exc:
            # причина у этого отказа не в файле, а в нас, и объяснение
            # пользователю нужно длиннее одной строки — см. _too_large_notice
            log.warning("вложение %s не взято в работу: %s", attachment.filename, exc)
            too_large.append(exc)
            continue
        except AttachmentError as exc:
            log.warning("вложение %s пропущено: %s", attachment.filename, exc)
            skipped.append(f"«{attachment.filename}»: {exc}")
            continue
        except Exception as exc:  # сеть, Open WebUI, таймаут обработки
            log.error("вложение %s не загружено: %s", attachment.filename, exc)
            skipped.append(f"«{attachment.filename}»: сервис документов недоступен")
            continue
        context.files.append(link)
        context.uploaded.append(file_id)
        descriptions.append(description)
        if not document.full_context:
            focused.append(f"документ «{document.filename}» ({document.size_label})")

    # Прежние файлы треда идут после новых: свежий документ письма важнее.
    # Общий потолок считается на весь запрос, а не на каждую половину отдельно,
    # иначе тред с десятком документов раздул бы запрос вдвое против настройки
    budget = max(0, ATTACHMENT_MAX_SESSION_FILES - len(context.files))
    stored_links, stored_notes = _describe_stored(stored[:budget])
    context.files += stored_links
    descriptions += stored_notes

    from src.attachments import context_block

    context.prompt_prefix = context_block(descriptions)
    if focused:
        context.notes.append(ATTACHMENT_FOCUSED_TEXT.format(details=", ".join(focused)))
    if too_large:
        context.notes.extend(_too_large_notes(too_large))
    if skipped:
        context.notes.append(ATTACHMENT_SKIPPED_TEXT.format(details="; ".join(skipped)))
    return context


def _drop_uploaded(file_ids: Sequence[str]) -> None:
    """Убрать за собой файлы, загруженные в примерке (dry-run).

    В dry-run состояние не меняется вообще — ни база, ни ящик. Файл в чужом
    хранилище тем более не должен переживать примерку: иначе десяток прогонов
    `once --dry-run` оставит в Open WebUI десяток копий одного документа,
    и удалить их будет нечем — в базе про них ничего не записано.
    """
    if not file_ids:
        return
    from src import owui_files

    for file_id in file_ids:
        owui_files.delete(file_id)


def _process_claimed(incoming: IncomingEmail, transport: MailTransport, dry_run: bool) -> Outcome:
    """Основная часть обработки — письмо уже застолблено в журнале.

    В dry-run состояние не меняется вообще: ни журнал, ни история. Это делает
    «примерку» повторяемой — прогон даёт один и тот же результат сколько угодно раз.
    """

    def record(status: str, detail: str) -> None:
        if not dry_run:
            storage.finish_message(incoming.message_id, status, detail)

    if storage.count_messages_last_hour(incoming.sender) >= RATE_LIMIT_PER_HOUR:
        _reply(incoming, incoming.title, RATE_LIMIT_NOTICE.format(limit=RATE_LIMIT_PER_HOUR), transport, dry_run)
        record("skipped", "rate limit")
        return Outcome("skipped", "превышен лимит писем в час")

    prompt = incoming.body
    if not prompt and not incoming.attachments:
        # winmail.dat выглядит как пустое письмо, но лечится совсем иначе,
        # поэтому подсказка нужна своя
        notice = TNEF_NOTICE if incoming.is_tnef else EMPTY_BODY_NOTICE
        _reply(incoming, incoming.title, notice, transport, dry_run)
        record("skipped", "тело в winmail.dat" if incoming.is_tnef else "пустое тело письма")
        return Outcome("skipped", "в письме нет текста")

    # Письмо из одних вложений — обычный случай: «см. вложение» пишут не всегда.
    # Вопроса в нём нет, поэтому вопрос подставляем сами, иначе модель получит
    # документ и пустую строку
    if not prompt:
        prompt = DOCUMENT_ONLY_PROMPT

    truncated = len(prompt) > MAX_PROMPT_CHARS
    if truncated:
        log.warning("письмо длиной %d символов обрезано до %d", len(prompt), MAX_PROMPT_CHARS)
        prompt = prompt[:MAX_PROMPT_CHARS]

    session_id = _resolve_session(incoming, dry_run)
    session = storage.get_session(session_id) if session_id else None
    title = session["title"] if session else incoming.title

    # Реплики одной сессии — строго по очереди: иначе два письма треда
    # прочитали бы одну историю и ответили каждое без учёта второго вопроса.
    # Разные сессии при этом обрабатываются параллельно.
    with _session_lock(session_id):
        # Вложения готовятся под блокировкой сессии: два письма одного треда,
        # пришедшие разом, иначе записали бы свои файлы вперемешку, и второе
        # увидело бы в истории документ, про который его ещё не спрашивали
        attachments_ctx = _prepare_attachments(incoming, session_id, dry_run)
        try:
            # Письмо из одних вложений, ни одно из которых до модели не доехало.
            # Вопроса нет, документа нет — спрашивать модель не о чем: она
            # ответила бы по пустому месту, а причина отказа выглядела бы
            # оговоркой к этому ответу. Человеку нужна ровно причина и что
            # делать дальше
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
            # в примерке файл не должен пережить прогон ни на одной ветке,
            # включая неожиданную ошибку: удалить его потом будет нечем —
            # в базе про него ничего не записано
            if dry_run:
                _drop_uploaded(attachments_ctx.uploaded)


def _answer(
    incoming: IncomingEmail,
    session_id: int,
    title: str,
    prompt: str,
    truncated: bool,
    attachments_ctx: "AttachmentContext",
    transport: MailTransport,
    dry_run: bool,
    record,
) -> Outcome:
    """История, запрос к модели и отправка — вложения к этому моменту готовы.

    Вынесено из `_process_claimed` не ради длины: загруженные файлы нужно убрать
    за собой в примерке на любой ветке выхода, а для этого у ветвления должно
    быть одно место — `finally` вызывающей функции.
    """
    from src import llm

    history = storage.get_history(session_id, MAX_HISTORY_MESSAGES) if session_id else []
    if not dry_run:
        # история берётся ДО записи текущего письма, иначе вопрос продублируется.
        # body_raw — тело вместе с цитатой, а в цитате едет вся прежняя переписка
        # треда, включая реплики людей, которые сервису не писали. По умолчанию
        # не храним: поле нужно только для разбора промахов эвристики цитат
        storage.add_message(
            session_id, "user", prompt, incoming.message_id,
            incoming.body_raw if STORE_RAW_BODY else "",
        )

    # Описание документов идёт в запрос, но не в историю сессии: в базе
    # остаётся то, что написал человек, а описание пересобирается из
    # session_files на каждом письме треда — иначе оно копилось бы
    # в каждой реплике и вытесняло саму переписку
    request_prompt = attachments_ctx.prompt_prefix + prompt
    try:
        answer = _retry(
            lambda: llm.generate(history, request_prompt, attachments_ctx.files),
            attempts=3,
            what="генерация ответа",
        )
    except Exception as exc:
        log.error("модель не ответила: %s", exc)
        _reply(incoming, title, LLM_ERROR_NOTICE.format(error=exc), transport, dry_run)
        storage.finish_message(incoming.message_id, "error", f"LLM: {exc}")
        if dry_run:
            _drop_uploaded(attachments_ctx.uploaded)
        return Outcome("error", f"модель недоступна: {exc}", session_id)

    if truncated:
        answer += TRUNCATION_NOTICE.format(limit=MAX_PROMPT_CHARS)
    answer += attachments_ctx.notice

    try:
        sent_message_id = _retry(
            lambda: _reply(incoming, title, answer, transport, dry_run),
            attempts=3,
            what="отправка письма",
        )
    except Exception as exc:
        # отправка чаще всего лечится сама: снимаем заявку, письмо остаётся
        # непрочитанным и попадёт в следующий проход
        log.error("не удалось отправить ответ: %s", exc)
        if not dry_run:
            storage.release_message(incoming.message_id)
        return Outcome("error", f"отправка: {exc}", session_id)

    if dry_run:
        return Outcome("ok", "ответ сгенерирован, письмо не отправлено (dry-run)")

    # ответ сохраняем только после успешной отправки: иначе ручной повтор
    # сгенерировал бы второй ответ на тот же вопрос
    storage.add_message(session_id, "assistant", answer, sent_message_id)
    record("ok", f"сессия {session_id}")
    return Outcome("ok", f"ответ отправлен в сессию {session_id}", session_id)


def run_once(
    transport: Optional[MailTransport] = None,
    dry_run: bool = False,
    workers: int = WORKERS,
) -> RunSummary:
    """Один проход по непрочитанным письмам.

    Письма обрабатываются пулом потоков: узкое место — генерация ответа, и при
    workers=1 второй сотрудник ждёт минуты, пока модель отвечает первому.

    Отметка «обработано» ставится в главном потоке после того, как пул отработал:
    сессия EWS не рассчитана на команды из нескольких потоков одновременно,
    а идемпотентность и без флага держится журналом.
    """
    own_transport = transport is None
    transport = transport or get_transport()
    summary = RunSummary()
    try:
        fetched = transport.fetch_unseen()
        if not fetched:
            return summary

        results: List[Tuple[object, Outcome]] = []
        if workers > 1 and len(fetched) > 1:
            log.info("писем в проходе: %d, обрабатываю в %d потоках", len(fetched), workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mail") as pool:
                futures = {
                    pool.submit(process_email, msg, dry_run, transport): handle
                    for handle, msg in fetched
                }
                for future in as_completed(futures):
                    results.append((futures[future], future.result()))
        else:
            for handle, msg in fetched:
                results.append((handle, process_email(msg, dry_run, transport)))

        for handle, outcome in results:
            summary.add(outcome)
            if MARK_SEEN and not dry_run and outcome.can_mark_seen:
                transport.mark_seen(handle)
    finally:
        if own_transport:
            transport.close()
    return summary


def run_forever(
    interval: int = POLL_INTERVAL_SEC, dry_run: bool = False, workers: int = WORKERS
) -> None:
    """Демон: опрос ящика по интервалу с переподключением при разрывах."""
    stale = storage.reset_stale_processing(LLM_TIMEOUT_SEC * 2)
    if stale:
        log.warning("%d писем зависли в обработке после прошлой остановки, помечены как error", stale)

    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("получен сигнал %s, останавливаюсь", signal.Signals(signum).name)
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    transport = get_transport()
    backoff = interval
    log.info(
        "демон запущен: %s, опрос каждые %d с, потоков %d%s, срок хранения %s",
        MAIL_ADDRESS, interval, workers, " [dry-run]" if dry_run else "",
        f"{RETENTION_DAYS} дней" if RETENTION_DAYS > 0 else "бессрочно",
    )

    # Чистка по срокам хранения идёт в самом демоне, а не внешним cron: иначе она
    # существует только там, где кто-то не забыл её настроить, — а сроки хранения
    # должны соблюдаться по построению. Первый прогон сразу на старте, дальше раз
    # в сутки; отметка держится в памяти, поэтому частые перезапуски демона
    # приводят к лишним прогонам, а не к пропущенным.
    next_purge = 0.0

    while running:
        try:
            if time.monotonic() >= next_purge:
                run_retention()
                next_purge = time.monotonic() + 24 * 3600
            summary = run_once(transport, dry_run=dry_run, workers=workers)
            if summary.fetched:
                log.info(
                    "обработано %d: ответов %d, пропущено %d, ошибок %d",
                    summary.fetched, summary.answered, summary.skipped, summary.failed,
                )
            backoff = interval
        except Exception as exc:
            # разрыв соединения, отвалившаяся сеть, таймаут — переподключаемся
            # с нарастающей паузой, чтобы не долбить сервер в цикле
            backoff = min(backoff * 2, 300)
            log.warning("сбой цикла (%s), переподключение через %d с", exc, backoff)
            try:
                transport.reconnect()
            except Exception:
                log.debug("переподключение не удалось, повтор на следующей итерации", exc_info=True)

        for _ in range(backoff):
            if not running:
                break
            time.sleep(1)

    transport.close()
    log.info("демон остановлен")


def run_retention() -> Tuple[int, int, int]:
    """Оба срока хранения за один проход. Возвращает (файлов, сессий, записей).

    Порядок обязателен: файлы сессий, которые вот-вот удалятся, снимаются
    в Open WebUI ДО удаления самих сессий. Каскад унёс бы строки `session_files`
    вместе с сессией, и файлы остались бы в чужом хранилище навсегда —
    без единой записи о том, чьи они и откуда взялись.
    """
    from src import owui_files

    files, _ = owui_files.purge_expired(ATTACHMENT_RETENTION_DAYS)
    if RETENTION_DAYS <= 0:
        return (files, 0, 0)

    files += owui_files.forget(storage.file_ids_of_expired_sessions(RETENTION_DAYS))
    sessions, journal = storage.purge_older_than(RETENTION_DAYS)
    return (files, sessions, journal)


def retry_failed(transport: Optional[MailTransport] = None) -> int:
    """Вернуть письма со статусом error в очередь.

    Записи журнала удаляются, а письма снова помечаются непрочитанными —
    следующий проход подберёт их обычным путём, без отдельной ветки обработки.
    """
    own_transport = transport is None
    transport = transport or get_transport()
    restored = 0
    try:
        for row in storage.list_failed():
            message_id = row["message_id"]
            if not transport.unsee_by_message_id(message_id):
                log.warning("письмо %s не найдено в папке — пропускаю", message_id)
                continue
            storage.forget_message(message_id)
            restored += 1
            log.info("возвращено в очередь: %s", message_id)
    finally:
        if own_transport:
            transport.close()
    return restored
