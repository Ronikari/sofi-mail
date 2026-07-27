"""Приём писем по IMAP.

Используется stdlib `imaplib`: разбор MIME всё равно делает email_parser, поэтому
от библиотек-обёрток осталась бы только авторизация. Зато здесь явно видны две
вещи, на которых обычно ломаются почтовые боты: работа по UID (порядковые номера
съезжают после любого изменения папки) и BODY.PEEK вместо BODY (обычный FETCH
сам ставит флаг \\Seen, и письмо теряется при падении до отправки ответа).
"""

import email
import imaplib
import logging
from email.message import Message
from typing import Callable, List, Optional, Tuple, TypeVar

from src.config import (
    IMAP_FOLDER,
    IMAP_HOST,
    IMAP_PORT,
    IMAP_STARTTLS,
    MAIL_LOGIN,
    MAIL_PASSWORD,
)
from src.tls import build_ssl_context

log = logging.getLogger(__name__)

T = TypeVar("T")


def _open() -> imaplib.IMAP4:
    """Соединение с сервером: 993 сразу по SSL или 143 с STARTTLS."""
    context = build_ssl_context()
    if IMAP_STARTTLS:
        conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        conn.starttls(ssl_context=context)
        return conn
    return imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context)


class IMAPClient:
    """Тонкая обёртка над imaplib с явным переподключением."""

    def __init__(self) -> None:
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self) -> None:
        log.debug("подключение к %s:%s как %s", IMAP_HOST, IMAP_PORT, MAIL_LOGIN)
        conn = _open()
        conn.login(MAIL_LOGIN, MAIL_PASSWORD)
        conn.select(IMAP_FOLDER)
        self._conn = conn

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
            self._conn.logout()
        except Exception:  # разорванное соединение закрывать нечем — не мешаем выходу
            log.debug("IMAP закрыт с ошибкой", exc_info=True)
        finally:
            self._conn = None

    def reconnect(self) -> None:
        self.close()
        self.connect()

    @property
    def conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _command(self, name: str, action: Callable[[], T]) -> T:
        """Выполнить IMAP-команду, пережив разрыв соединения.

        Пока модель генерирует ответ (минуты), по IMAP не идёт ни одной команды,
        и сервер закрывает простаивающий сокет — очередная команда падает с
        `socket error: EOF`. Это норма для почтовых серверов, а не сбой: UID-ы
        стабильны между сессиями, поэтому команду достаточно повторить на новом
        соединении. Иначе разрыв ронял бы весь проход демона.

        Повтор ровно один: если и он не удался, соединения действительно нет —
        пусть цикл уходит на backoff, а не долбит сервер переподключениями.
        """
        try:
            return action()
        except (imaplib.IMAP4.abort, OSError) as exc:
            log.warning("IMAP-соединение разорвано на %s (%s), переподключаюсь", name, exc)
            self.reconnect()
            return action()

    def fetch_unseen(self) -> List[Tuple[bytes, Message]]:
        """Непрочитанные письма как (uid, разобранное сообщение).

        Флаг \\Seen не ставится: письмо считается обработанным только после
        успешной отправки ответа (см. pipeline).
        """
        status, data = self._command("SEARCH", lambda: self.conn.uid("SEARCH", None, "UNSEEN"))
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH вернул {status}")

        uids = data[0].split() if data and data[0] else []
        messages: List[Tuple[bytes, Message]] = []
        for uid in uids:
            status, payload = self._command("FETCH", lambda: self.conn.uid("FETCH", uid, "(BODY.PEEK[])"))
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("не удалось получить письмо uid=%s", uid.decode())
                continue
            messages.append((uid, email.message_from_bytes(payload[0][1])))
        return messages

    def mark_seen(self, uid: bytes) -> None:
        self._command("STORE", lambda: self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)"))

    def unsee_by_message_id(self, message_id: str) -> int:
        """Снять \\Seen с письма по его Message-ID. Возвращает число писем.

        Так команда `retry` возвращает письмо в очередь: следующий проход
        подберёт его обычным путём, без отдельной ветки обработки.
        """
        status, data = self._command(
            "SEARCH", lambda: self.conn.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
        )
        uids = data[0].split() if status == "OK" and data and data[0] else []
        for uid in uids:
            self._command("STORE", lambda: self.conn.uid("STORE", uid, "-FLAGS", "(\\Seen)"))
        return len(uids)


def check_imap() -> str:
    """Проверка логина и доступности папки — для команды `check`."""
    conn = _open()
    try:
        conn.login(MAIL_LOGIN, MAIL_PASSWORD)
        status, data = conn.select(IMAP_FOLDER)
        if status != "OK":
            raise RuntimeError(f"папка {IMAP_FOLDER} недоступна: {data}")
        total = int(data[0]) if data and data[0] else 0
        status, unseen = conn.uid("SEARCH", None, "UNSEEN")
        pending = len(unseen[0].split()) if status == "OK" and unseen and unseen[0] else 0
        return f"{IMAP_HOST}:{IMAP_PORT}, папка {IMAP_FOLDER}: {total} писем, {pending} непрочитанных"
    finally:
        try:
            conn.logout()
        except Exception:
            pass
