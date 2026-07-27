"""Отправка ответного письма по SMTP."""

import email.utils
import logging
import smtplib
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Iterator, List, Optional

from src.config import (
    LLM_MODEL,
    MAIL_ADDRESS,
    MAIL_DISPLAY_NAME,
    MAIL_LOGIN,
    MAIL_PASSWORD,
    SMTP_AUTH,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_TLS,
)
from src.email_parser import LOOP_HEADER, REPLY_MARKER
from src.tls import build_ssl_context

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


@contextmanager
def _server(timeout: int) -> Iterator[smtplib.SMTP]:
    """Подключение и, если нужно, авторизация.

    Соединение открывается на каждую отправку заново: это делает отправку
    потокобезопасной (одно `smtplib.SMTP` на несколько потоков не переживёт
    параллельных писем) и снимает вопрос простаивающих сессий.

    SMTP_AUTH=false — для коннектора внутреннего релея Exchange: авторизацию он
    не объявляет, и `login()` падает с SMTPNotSupportedError, хотя письмо ушло
    бы и без неё.
    """
    context = build_ssl_context() if SMTP_TLS != "none" else None
    if SMTP_TLS == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout)
    try:
        if SMTP_TLS == "starttls":
            server.starttls(context=context)
        if SMTP_AUTH:
            server.login(MAIL_LOGIN, MAIL_PASSWORD)
        yield server
    finally:
        try:
            server.quit()
        except Exception:  # сервер мог отвалиться раньше — выходу это не мешает
            log.debug("SMTP закрыт с ошибкой", exc_info=True)


def send(message: EmailMessage) -> None:
    """Отправка письма. Исключения пробрасываются — ретраями ведает пайплайн."""
    with _server(timeout=30) as server:
        server.send_message(message)
    log.info("отправлено -> %s: %s", message["To"], message["Subject"])


def send_reply(
    to_address: str,
    subject: str,
    body: str,
    session_title: str,
    in_reply_to: Optional[str] = None,
    references: Optional[List[str]] = None,
) -> str:
    """Собрать и отправить ответ. Возвращает Message-ID отправленного письма."""
    message = build_reply(to_address, subject, body, session_title, in_reply_to, references)
    send(message)
    return message["Message-ID"]


def check_smtp() -> str:
    """Проверка логина на SMTP — для команды `check`."""
    try:
        with _server(timeout=15):
            pass
    except TimeoutError as exc:
        # таймаут — это не «неверный пароль», а закрытый порт: исходящий SMTP
        # режут VPN, провайдеры и корпоративные сети. Подсказка экономит часы
        # поисков ошибки в пароле приложения
        raise RuntimeError(
            f"{SMTP_HOST}:{SMTP_PORT} не отвечает ({exc}). Порт не открыт: проверьте VPN "
            f"и фильтрацию сети — `nc -vz {SMTP_HOST} {SMTP_PORT}`. Пароль тут не при чём: "
            "при неверном пароле сервер отвечает ошибкой авторизации, а не молчит"
        ) from exc
    return f"{SMTP_HOST}:{SMTP_PORT} как {MAIL_ADDRESS}"
