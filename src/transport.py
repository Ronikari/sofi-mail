# Контракт почтового транспорта и фабрика его реализации (EWSTransport),
# чтобы pipeline.py работал с почтой через единый протокол, не зная деталей EWS.

import logging
from email.message import Message
from typing import Any, List, Optional, Protocol, Sequence, Tuple

log = logging.getLogger(__name__)

# Пара «дескриптор письма, разобранный MIME»; дескриптор непрозрачен для pipeline.py
# и просто возвращается обратно в mark_seen.
FetchedEmail = Tuple[Any, Message]


# Набор операций, которыми pipeline.py пользуется при работе с почтой.
class MailTransport(Protocol):
    # выход: список необработанных писем, порядок задаёт реализация
    def fetch_unseen(self) -> List[FetchedEmail]: ...

    # вход: дескриптор из fetch_unseen. вызов допустим после отправки ответа
    def mark_seen(self, handle: Any) -> None: ...

    # вход: дескрипторы писем одной пачки. отмечает их за один запрос
    # к серверу; вызов допустим после отправки ответов на все эти письма
    def mark_seen_bulk(self, handles: Sequence[Any]) -> None: ...

    # выход: число писем, снятых с признака прочитанности. используется командой retry
    def unsee_by_message_id(self, message_id: str) -> int: ...

    # Выход: Message-ID отправленного письма. Параметры треда/разговора нужны, чтобы Outlook
    # сгруппировал ответ с входящим письмом; без quoted_body/quoted_html ответ уходит без цитаты.
    def send_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        session_title: str,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
        thread_index: str = "",
        incoming_topic: str = "",
        sender_name: str = "",
        quoted_body: str = "",
        sent_date: str = "",
        quoted_html: str = "",
    ) -> str: ...

    # пересоздаёт соединение после разрыва, вызывается из цикла демона
    def reconnect(self) -> None: ...

    def close(self) -> None: ...

    # выход: строка о состоянии ящика для команды check
    def describe(self) -> str: ...


# Выход: объект EWSTransport (соединение открывается при первом обращении) — EWS
# работает в Exchange постоянно, в отличие от отдельно включаемого IMAP4.
def get_transport() -> MailTransport:
    """Создаёт транспорт поверх Exchange Web Services."""
    # импорт по месту вызова: ews_client.py тянет exchangelib, а тот requests,
    # lxml и pyspnego. команды sessions, history и --help почту не открывают
    from src.ews_client import EWSTransport

    return EWSTransport()
