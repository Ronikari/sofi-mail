# контракт почтового транспорта и фабрика его реализации.
# порядок: pipeline.py запрашивает транспорт у get_transport() -> получает объект
# EWSTransport -> вызывает пять операций протокола MailTransport.
# вход: настройки почты читает сама реализация из config.py.
# выход: объект, удовлетворяющий MailTransport; письма отдаются парой
# (дескриптор, разобранный MIME).
# реализация лежит в ews_client.py и импортируется по месту вызова.
# вызывается из pipeline.py и cli.py.

import logging
from email.message import Message
from typing import Any, List, Optional, Protocol, Sequence, Tuple

log = logging.getLogger(__name__)

# пара «дескриптор письма, разобранный MIME». дескриптор непрозрачен для
# pipeline.py: у EWS это объект письма exchangelib, pipeline возвращает его
# обратно в mark_seen
FetchedEmail = Tuple[Any, Message]


# набор операций, которыми pipeline.py пользуется при работе с почтой.
# протокол (Protocol) описывает форму объекта без наследования, тела методов
# состоят из литерала ...
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

    # выход: Message-ID отправленного письма, пишется в таблицу messages.
    # in_reply_to и references задают заголовки треда, thread_index
    # и incoming_topic — заголовки разговора Exchange: по ним Outlook показывает
    # письмо ответом на входящее, а не отдельной перепиской
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
    ) -> str: ...

    # пересоздаёт соединение после разрыва, вызывается из цикла демона
    def reconnect(self) -> None: ...

    def close(self) -> None: ...

    # выход: строка о состоянии ящика для команды check
    def describe(self) -> str: ...


# выход: объект EWSTransport, соединение открывается при первом обращении.
# EWS (Exchange Web Services) — протокол, которым работает Outlook; в Exchange
# он включён постоянно, служба MSExchangeIMAP4 запускается отдельно
def get_transport() -> MailTransport:
    """Создаёт транспорт поверх Exchange Web Services."""
    # импорт по месту вызова: ews_client.py тянет exchangelib, а тот requests,
    # lxml и pyspnego. команды sessions, history и --help почту не открывают
    from src.ews_client import EWSTransport

    return EWSTransport()
