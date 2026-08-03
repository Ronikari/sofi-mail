"""Сборка ответного письма (MIME).

Только сборка, без отправки: `EWSTransport.send_reply` отдаёт Exchange готовый
MIME, собранный этим кодом. Благодаря этому заголовки треда, подпись-маркер
и заранее сгенерированный Message-ID собираются в одном месте и их видно
целиком, а не по кускам внутри работы с Exchange.
"""

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

# References длиннее этого некоторые MTA обрезают по-своему, ломая тред;
# по общепринятой практике оставляем корень цепочки и ближайших предков
MAX_REFERENCES = 20


def build_footer(session_title: str) -> str:
    """Подпись с техническим маркером.

    Маркер нужен не для красоты: по нему мы отрезаем цитату, когда пользователь
    отвечает на это письмо (см. email_parser.strip_quoted).
    """
    return f"-- \n{REPLY_MARKER} {MAIL_DISPLAY_NAME} · {LLM_MODEL} · сессия «{session_title}»"


def build_reply(
    to_address: str,
    subject: str,
    body: str,
    session_title: str,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> EmailMessage:
    """Ответное письмо с заголовками, склеивающими тред у получателя."""
    message = EmailMessage()
    message["From"] = email.utils.formataddr((MAIL_DISPLAY_NAME, MAIL_ADDRESS))
    message["To"] = to_address
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message["Date"] = email.utils.formatdate(localtime=True)
    # Message-ID генерируем заранее: его нужно сохранить в БД до отправки,
    # иначе ответ пользователя на это письмо не найдёт свою сессию
    message["Message-ID"] = email.utils.make_msgid(domain=MAIL_ADDRESS.split("@")[-1] or None)

    if in_reply_to:
        # оба заголовка обязательны: Gmail собирает тред преимущественно
        # по References, Outlook — по In-Reply-To
        message["In-Reply-To"] = in_reply_to
        chain = list(references or [])
        if in_reply_to not in chain:
            chain.append(in_reply_to)
        if len(chain) > MAX_REFERENCES:
            chain = chain[:1] + chain[-(MAX_REFERENCES - 1):]
        message["References"] = " ".join(chain)

    # чтобы автоответчики и почтовые серверы на той стороне не устроили петлю
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    # своя метка: если письмо вернётся к нам, петлю видно сразу
    message[LOOP_HEADER] = "1"

    message.set_content(f"{body}\n\n{build_footer(session_title)}\n", subtype="plain", charset="utf-8")
    return message
