"""Транспорт почты: единый контракт для IMAP/SMTP и EWS.

Пайплайн знает о почте ровно пять операций и ничего не знает о протоколе.
Благодаря этому поддержка Exchange добавляется отдельным модулем, а не
ветвлениями по всему коду, и обе реализации собирают ответное письмо одним
и тем же `smtp_client.build_reply` — заголовки треда и подпись-маркер
одинаковы, поэтому и разбор ответов пользователя одинаков.

Дескриптор письма (`handle`) намеренно непрозрачен: у IMAP это UID, у EWS —
объект письма. Пайплайн только возвращает его обратно в `mark_seen`.
"""

import logging
from email.message import Message
from typing import Any, List, Optional, Protocol, Tuple

from src.config import MAIL_TRANSPORT

log = logging.getLogger(__name__)

FetchedEmail = Tuple[Any, Message]


class MailTransport(Protocol):
    """Что пайплайну нужно от почты."""

    def fetch_unseen(self) -> List[FetchedEmail]:
        """Необработанные письма как (дескриптор, разобранный MIME)."""

    def mark_seen(self, handle: Any) -> None:
        """Отметить письмо обработанным — только после отправки ответа."""

    def unsee_by_message_id(self, message_id: str) -> int:
        """Вернуть письмо в очередь по Message-ID (для команды `retry`)."""

    def send_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        session_title: str,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
    ) -> str:
        """Отправить ответ, вернуть Message-ID отправленного письма."""

    def reconnect(self) -> None:
        """Пересобрать соединение после разрыва."""

    def close(self) -> None: ...

    def describe(self) -> str:
        """Строка о состоянии ящика — для команды `check`."""


class SmtpImapTransport:
    """Приём по IMAP, отправка по SMTP.

    Публичные провайдеры (Gmail, Яндекс, Mail.ru) и Exchange с включённой
    службой IMAP4 и разрешённой Basic-аутентификацией.
    """

    def __init__(self) -> None:
        from src.imap_client import IMAPClient

        self._imap = IMAPClient()

    def fetch_unseen(self) -> List[FetchedEmail]:
        return list(self._imap.fetch_unseen())

    def mark_seen(self, handle: Any) -> None:
        self._imap.mark_seen(handle)

    def unsee_by_message_id(self, message_id: str) -> int:
        return self._imap.unsee_by_message_id(message_id)

    def send_reply(self, **kwargs) -> str:
        # отправка отдельным соединением на каждое письмо — так она
        # потокобезопасна и не зависит от состояния IMAP-сессии
        from src import smtp_client

        return smtp_client.send_reply(**kwargs)

    def reconnect(self) -> None:
        self._imap.reconnect()

    def close(self) -> None:
        self._imap.close()

    def describe(self) -> str:
        from src.imap_client import check_imap
        from src.smtp_client import check_smtp

        return f"{check_imap()}; отправка: {check_smtp()}"


def get_transport() -> MailTransport:
    """Транспорт по MAIL_TRANSPORT из .env."""
    if MAIL_TRANSPORT == "ews":
        from src.ews_client import EWSTransport

        log.debug("транспорт: EWS")
        return EWSTransport()

    if MAIL_TRANSPORT != "imap":
        raise ValueError(f"MAIL_TRANSPORT={MAIL_TRANSPORT!r}: допустимы 'imap' и 'ews'")

    log.debug("транспорт: IMAP + SMTP")
    return SmtpImapTransport()
