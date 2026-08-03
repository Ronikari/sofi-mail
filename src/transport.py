"""Транспорт почты: контракт, которым пользуется пайплайн.

Пайплайн знает о почте ровно пять операций и ничего не знает о протоколе,
поэтому логика обработки письма не перемешана с деталями Exchange.

Реализация одна — `EWSTransport` (Exchange Web Services): тот же протокол,
которым ходит Outlook, и единственный, который в Exchange включён всегда.

Дескриптор письма (`handle`) намеренно непрозрачен: у EWS это объект письма.
Пайплайн только возвращает его обратно в `mark_seen`.
"""

import logging
from email.message import Message
from typing import Any, List, Optional, Protocol, Tuple

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


def get_transport() -> MailTransport:
    """Подключение к почте.

    Импорт внутри функции: `exchangelib` тянет за собой `requests`, `lxml`
    и `pyspnego`, а команды вроде `sessions` и `--help` работают без почты
    и ждать их загрузки не должны.
    """
    from src.ews_client import EWSTransport

    return EWSTransport()
