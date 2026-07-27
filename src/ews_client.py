"""Приём и отправка через Exchange Web Services.

Нужен там, где IMAP и SMTP закрыты: службу `MSExchangeIMAP4` в Exchange 2013+
по умолчанию не запускают, а Basic-аутентификацию на почтовых протоколах часто
отключают в пользу NTLM/Kerberos или OAuth2. EWS при этом остаётся включённым
всегда — через него работает сам Outlook.

Два решения определяют этот модуль.

Первое: письма забираются и отправляются **сырым MIME** (`mime_content`).
Поэтому `email_parser` и сборка ответа (`smtp_client.build_reply`) переиспользуются
без изменений, а наш заранее сгенерированный `Message-ID` доезжает до сервера
как есть — иначе его пришлось бы вычитывать из «Отправленных» после отправки,
и сопоставление тредов повисло бы на фоллбэке по теме.

Второе: `exchangelib` не входит в обязательные зависимости и импортируется
внутри функций. Проект должен ставиться и работать на публичной почте без
корпоративного стека (`requests`, `lxml`, `pyspnego` и прочего).
"""

import email
import logging
import os
import threading
from email.message import Message
from typing import Any, List, Optional, Tuple

from src.config import (
    EWS_ACCESS_TYPE,
    EWS_AUTH,
    EWS_CLIENT_ID,
    EWS_CLIENT_SECRET,
    EWS_ENDPOINT,
    EWS_FOLDER,
    EWS_SERVER,
    EWS_TENANT_ID,
    MAIL_ADDRESS,
    MAIL_CA_FILE,
    MAIL_LOGIN,
    MAIL_PASSWORD,
    MAIL_TLS_VERIFY,
    WORKERS,
)

log = logging.getLogger(__name__)

# поля, которые реально нужны: сырой MIME для разбора и служебные для is_read.
# Без явного .only() exchangelib тянет десятки свойств на каждое письмо
_FETCH_FIELDS = ("id", "changekey", "mime_content", "datetime_received", "subject")

# Kerberos/SSPI ходят по билету из кеша — пароль в них не участвует
_PASSWORDLESS_AUTH = ("gssapi", "sspi")


def _auth_type() -> Optional[str]:
    """Строка из .env в константу exchangelib. Пусто — пусть определит сам."""
    from exchangelib import BASIC, CBA, DIGEST, GSSAPI, NTLM, OAUTH2, SSPI

    known = {
        "basic": BASIC,
        "ntlm": NTLM,
        "gssapi": GSSAPI,
        "sspi": SSPI,
        "digest": DIGEST,
        "cba": CBA,
        "oauth2": OAUTH2,
    }
    if not EWS_AUTH:
        return None
    if EWS_AUTH not in known:
        raise ValueError(f"EWS_AUTH={EWS_AUTH!r}: допустимы {', '.join(sorted(known))}")
    return known[EWS_AUTH]


def _apply_tls_policy() -> None:
    """Политика TLS для HTTP-транспорта exchangelib.

    exchangelib ходит через `requests`, поэтому наш ssl-контекст ему не подходит:
    отказ от проверки задаётся подменой HTTP-адаптера, а внутренний УЦ —
    переменной окружения, которую уважает `requests`.
    """
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

    if not MAIL_TLS_VERIFY:
        log.warning("проверка TLS-сертификата EWS отключена (MAIL_TLS_VERIFY=false)")
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
    elif MAIL_CA_FILE:
        os.environ.setdefault("REQUESTS_CA_BUNDLE", MAIL_CA_FILE)


def _credentials():
    """Учётные данные под выбранный способ аутентификации.

    OAuth2 распадается на два потока, и выбор между ними — по наличию пароля:

    * пароль задан — приложение действует **от имени пользователя** (ROPC):
      логин и пароль обмениваются на токен, доступ остаётся delegate;
    * пароля нет — приложение действует **от своего имени** (client credentials):
      токен выдаётся регистрации, а нужный ящик указывается заголовком
      impersonation, для чего в учётные данные и кладётся Identity.

    Второй вариант и есть режим службы: пароль пользователя нигде не хранится,
    а ROPC в тенантах часто запрещён политикой.
    """
    from exchangelib import Credentials, Identity, OAuth2Credentials, OAuth2LegacyCredentials

    if EWS_AUTH in _PASSWORDLESS_AUTH:
        return None  # Kerberos/SSPI берут билет из кеша

    if EWS_AUTH != "oauth2":
        return Credentials(username=MAIL_LOGIN, password=MAIL_PASSWORD)

    if MAIL_PASSWORD:
        log.debug("EWS: OAuth2 от имени пользователя %s", MAIL_LOGIN)
        return OAuth2LegacyCredentials(
            username=MAIL_LOGIN,
            password=MAIL_PASSWORD,
            client_id=EWS_CLIENT_ID,
            client_secret=EWS_CLIENT_SECRET,
            tenant_id=EWS_TENANT_ID or None,
        )

    log.debug("EWS: OAuth2 от имени приложения, ящик %s", MAIL_ADDRESS)
    return OAuth2Credentials(
        client_id=EWS_CLIENT_ID,
        client_secret=EWS_CLIENT_SECRET,
        tenant_id=EWS_TENANT_ID or None,
        identity=Identity(primary_smtp_address=MAIL_ADDRESS),
    )


def _build_account():
    """Подключение к ящику: явный сервер или autodiscover по адресу."""
    from exchangelib import Account, Configuration, DELEGATE, IMPERSONATION

    _apply_tls_policy()

    credentials = _credentials()
    access_type = IMPERSONATION if EWS_ACCESS_TYPE == "impersonation" else DELEGATE

    if EWS_SERVER or EWS_ENDPOINT:
        config = Configuration(
            credentials=credentials,
            server=EWS_SERVER or None,
            service_endpoint=EWS_ENDPOINT or None,
            auth_type=_auth_type(),
            # пул соединений под воркеров: иначе параллельная обработка
            # упирается в дефолтный лимит и письма ждут друг друга на HTTP
            max_connections=WORKERS + 1,
        )
        log.debug("EWS: явный сервер %s", EWS_ENDPOINT or EWS_SERVER)
        return Account(
            primary_smtp_address=MAIL_ADDRESS,
            config=config,
            autodiscover=False,
            access_type=access_type,
        )

    log.debug("EWS: autodiscover по адресу %s", MAIL_ADDRESS)
    return Account(
        primary_smtp_address=MAIL_ADDRESS,
        credentials=credentials,
        autodiscover=True,
        access_type=access_type,
    )


class EWSTransport:
    """Транспорт поверх EWS. Контракт — как у SmtpImapTransport."""

    def __init__(self) -> None:
        self._account = None
        # exchangelib потокобезопасен на уровне запросов, но само создание
        # Account (autodiscover, определение версии сервера) — нет
        self._lock = threading.Lock()

    # --- соединение ---------------------------------------------------------

    @property
    def account(self):
        with self._lock:
            if self._account is None:
                self._account = _build_account()
            return self._account

    def reconnect(self) -> None:
        with self._lock:
            self._account = None
        self.account  # noqa: B018 — поднимаем соединение сразу, чтобы упасть здесь, а не в цикле

    def close(self) -> None:
        with self._lock:
            if self._account is not None:
                try:
                    self._account.protocol.close()
                except Exception:
                    log.debug("EWS закрыт с ошибкой", exc_info=True)
                self._account = None

    def _folder(self):
        """Папка приёма: `inbox` или путь вида «Входящие/LLM»."""
        folder = self.account.inbox
        if EWS_FOLDER and EWS_FOLDER.lower() != "inbox":
            for part in EWS_FOLDER.split("/"):
                folder = folder / part
        return folder

    # --- приём -------------------------------------------------------------

    def fetch_unseen(self) -> List[Tuple[Any, Message]]:
        """Непрочитанные письма как (дескриптор, разобранный MIME).

        Дескриптор — сам объект письма exchangelib: по нему потом ставится
        is_read. Флаг здесь не выставляется: письмо считается обработанным
        только после успешной отправки ответа (см. pipeline).
        """
        items = (
            self._folder()
            .filter(is_read=False)
            .only(*_FETCH_FIELDS)
            .order_by("datetime_received")
        )

        result: List[Tuple[Any, Message]] = []
        for item in items:
            if not getattr(item, "mime_content", None):
                # календарные приглашения и прочие не-письма MIME не отдают
                log.warning("письмо без mime_content пропущено: %s", getattr(item, "subject", "?"))
                continue
            result.append((item, email.message_from_bytes(item.mime_content)))
        return result

    def mark_seen(self, handle: Any) -> None:
        handle.is_read = True
        handle.save(update_fields=["is_read"])

    def unsee_by_message_id(self, message_id: str) -> int:
        """Вернуть письмо в очередь: снять признак прочитанности по Message-ID."""
        count = 0
        for item in self._folder().filter(message_id=message_id).only("id", "changekey", "is_read"):
            item.is_read = False
            item.save(update_fields=["is_read"])
            count += 1
        return count

    # --- отправка ----------------------------------------------------------

    def send_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        session_title: str,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
    ) -> str:
        """Отправить ответ и вернуть Message-ID отправленного письма.

        Письмо собирается тем же кодом, что и для SMTP, и уходит как готовый
        MIME: заголовки треда, подпись-маркер и Message-ID одинаковы на обоих
        транспортах, а значит и разбор ответов пользователя одинаков.
        """
        from exchangelib import Message as EWSMessage

        from src.smtp_client import build_reply

        mime = build_reply(to_address, subject, body, session_title, in_reply_to, references)
        message_id = mime["Message-ID"]

        item = EWSMessage(account=self.account, mime_content=mime.as_bytes())
        item.send_and_save()
        log.info("отправлено (EWS) -> %s: %s", to_address, mime["Subject"])
        return message_id

    # --- диагностика -------------------------------------------------------

    def describe(self) -> str:
        folder = self._folder()
        return (
            f"EWS {self.account.protocol.service_endpoint}, ящик {MAIL_ADDRESS}, "
            f"папка {folder.name}: {folder.total_count} писем, {folder.unread_count} непрочитанных"
        )
