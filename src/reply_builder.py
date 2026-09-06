# сборка MIME-сообщения с ответом модели.
# порядок: текст ответа и данные треда -> заголовки From/To/Subject/Date ->
# генерация Message-ID -> заголовки треда In-Reply-To и References ->
# заголовки подавления автоответов -> тело с подписью.
# вход: адрес получателя, тема входящего письма, текст ответа модели, название
# сессии, Message-ID входящего письма и его цепочка References.
# выход: объект EmailMessage; ews_client.py отдаёт его Exchange байтами.
# MAIL_ADDRESS, MAIL_DISPLAY_NAME и LLM_MODEL импортируются из config.py,
# REPLY_MARKER и LOOP_HEADER — из email_parser.py.
# вызывается из ews_client.py, метод EWSTransport.send_reply.

import email.utils
import logging
from email.message import EmailMessage
from typing import List, Optional

from src.config import (
    LLM_MODEL,
    MAIL_ADDRESS,
    MAIL_DISPLAY_NAME,
)
from src.email_parser import LOOP_HEADER, REPLY_MARKER

log = logging.getLogger(__name__)

# предел длины цепочки References. часть почтовых серверов обрезает более
# длинную цепочку по своим правилам, и тред у получателя распадается.
# по общепринятой практике сохраняются корень цепочки и ближайшие предки
MAX_REFERENCES = 20


# выход: две строки — разделитель подписи и строка с REPLY_MARKER.
# строка с маркером служит границей при разборе ответа пользователя:
# email_parser.strip_quoted отрезает по ней цитату
def build_footer(session_title: str) -> str:
    """Собирает подпись письма с техническим маркером и названием сессии."""
    return f"-- \n{REPLY_MARKER} {MAIL_DISPLAY_NAME} · {LLM_MODEL} · сессия «{session_title}»"


# вход: to_address — адрес пользователя; subject — тема входящего письма;
# body — текст ответа модели; in_reply_to и references — заголовки треда
# из входящего письма, при первом письме сессии равны None.
# выход: EmailMessage с заполненным Message-ID; значение заголовка читает
# ews_client.py и передаёт в storage.add_message.
# побочные эффекты отсутствуют, сеть не используется
def build_reply(
    to_address: str,
    subject: str,
    body: str,
    session_title: str,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> EmailMessage:
    """Собирает ответное письмо с заголовками, склеивающими тред у получателя."""
    message = EmailMessage()

    # formataddr даёт форму «Имя <адрес>» с корректным кодированием имени
    message["From"] = email.utils.formataddr((MAIL_DISPLAY_NAME, MAIL_ADDRESS))
    message["To"] = to_address

    # префикс Re: добавляется к теме, которая его ещё не содержит; почтовые
    # клиенты накладывают такие префиксы каскадом
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    # formatdate(localtime=True) даёт дату в формате RFC 5322 с часовым поясом машины
    message["Date"] = email.utils.formatdate(localtime=True)

    # значение Message-ID создаётся до передачи письма Exchange: строка с ним
    # пишется в таблицу messages, и заголовок In-Reply-To входящего ответа
    # сопоставляется с этой строкой в storage.find_session_by_message_ids.
    # домен для идентификатора берётся из части MAIL_ADDRESS после @
    message["Message-ID"] = email.utils.make_msgid(domain=MAIL_ADDRESS.split("@")[-1] or None)

    # ветка первого письма сессии заголовки треда пропускает: предка нет
    if in_reply_to:
        # заполняются оба заголовка: Gmail собирает тред по References,
        # Outlook по In-Reply-To
        message["In-Reply-To"] = in_reply_to

        # цепочка предков дополняется идентификатором входящего письма
        chain = list(references or [])
        if in_reply_to not in chain:
            chain.append(in_reply_to)

        # срез оставляет корень треда и MAX_REFERENCES-1 ближайших предков,
        # середина цепочки отбрасывается
        if len(chain) > MAX_REFERENCES:
            chain = chain[:1] + chain[-(MAX_REFERENCES - 1):]

        # заголовок хранит идентификаторы через пробел, формат задан RFC 5322
        message["References"] = " ".join(chain)

    # заголовки RFC 3834 останавливают автоответчик на стороне получателя:
    # без них пара автоответчиков образует бесконечный обмен письмами
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"

    # собственная метка: письмо с ней, пришедшее во входящие, отбрасывается
    # в email_parser.automated_reason
    message[LOOP_HEADER] = "1"

    # тело письма: текст ответа, пустая строка, подпись с маркером
    message.set_content(f"{body}\n\n{build_footer(session_title)}\n", subtype="plain", charset="utf-8")
    return message
