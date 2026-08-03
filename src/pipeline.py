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
from dataclasses import dataclass
from email.message import Message
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

from src import redact, storage
from src.config import (
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

    Оба поиска ограничены адресом отправителя: сессия — это переписка с одним
    человеком, и письмо с чужого адреса не должно продолжать её ни по теме,
    ни по заголовкам треда.
    """
    session_id = storage.find_session_by_message_ids(incoming.ancestor_ids, incoming.sender)
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
        session_id = storage.create_session(incoming.title, incoming.sender, incoming.message_id)
        log.info(
            "новая сессия %s: %s с %s",
            session_id, redact.subject(incoming.title), redact.email_addr(incoming.sender),
        )
        return session_id


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
    if not prompt:
        # winmail.dat выглядит как пустое письмо, но лечится совсем иначе,
        # поэтому подсказка нужна своя
        notice = TNEF_NOTICE if incoming.is_tnef else EMPTY_BODY_NOTICE
        _reply(incoming, incoming.title, notice, transport, dry_run)
        record("skipped", "тело в winmail.dat" if incoming.is_tnef else "пустое тело письма")
        return Outcome("skipped", "в письме нет текста")

    truncated = len(prompt) > MAX_PROMPT_CHARS
    if truncated:
        log.warning("письмо длиной %d символов обрезано до %d", len(prompt), MAX_PROMPT_CHARS)
        prompt = prompt[:MAX_PROMPT_CHARS]

    session_id = _resolve_session(incoming, dry_run)
    session = storage.get_session(session_id) if session_id else None
    title = session["title"] if session else incoming.title

    from src import llm

    # Реплики одной сессии — строго по очереди: иначе два письма треда
    # прочитали бы одну историю и ответили каждое без учёта второго вопроса.
    # Разные сессии при этом обрабатываются параллельно.
    with _session_lock(session_id):
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

        try:
            answer = _retry(lambda: llm.generate(history, prompt), attempts=3, what="генерация ответа")
        except Exception as exc:
            log.error("модель не ответила: %s", exc)
            _reply(incoming, title, LLM_ERROR_NOTICE.format(error=exc), transport, dry_run)
            storage.finish_message(incoming.message_id, "error", f"LLM: {exc}")
            return Outcome("error", f"модель недоступна: {exc}", session_id)

        if truncated:
            answer += TRUNCATION_NOTICE.format(limit=MAX_PROMPT_CHARS)

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

    # Чистка по сроку хранения идёт в самом демоне, а не внешним cron: иначе она
    # существует только там, где кто-то не забыл её настроить, — а срок хранения
    # должен соблюдаться по построению. Первый прогон сразу на старте, дальше раз
    # в сутки; отметка держится в памяти, поэтому частые перезапуски демона
    # приводят к лишним прогонам, а не к пропущенным.
    next_purge = 0.0

    while running:
        try:
            if RETENTION_DAYS > 0 and time.monotonic() >= next_purge:
                storage.purge_older_than(RETENTION_DAYS)
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
