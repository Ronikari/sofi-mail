"""Транспорт EWS без обращения к Exchange.

Проверяется контракт, а не сеть: письмо уходит готовым MIME из `reply_builder`,
поэтому заголовки треда и наш Message-ID должны доезжать до Exchange как есть.
"""

import email

import pytest

pytest.importorskip("exchangelib", reason="exchangelib не установлен")

from src import ews_client  # noqa: E402
from src.email_parser import LOOP_HEADER, REPLY_MARKER, decode_mime_header  # noqa: E402


class FakeQuery:
    """Заглушка QuerySet exchangelib: цепочка filter/only/order_by."""

    def __init__(self, items):
        self.items = items
        self.filters = []

    def filter(self, **kwargs):
        self.filters.append(kwargs)
        return self

    def only(self, *fields):
        return self

    def order_by(self, *fields):
        return self

    def __iter__(self):
        return iter(self.items)


class FakeFolder:
    name = "Входящие"
    total_count = 3
    unread_count = 1

    def __init__(self, items):
        self.query = FakeQuery(items)

    def filter(self, **kwargs):
        return self.query.filter(**kwargs)


class FakeItem:
    def __init__(self, mime_content=None, subject="тема", message_id=None):
        self.mime_content = mime_content
        self.subject = subject
        self.message_id = message_id
        self.is_read = False
        self.saved_fields = None

    def save(self, update_fields=None):
        self.saved_fields = update_fields


class FakeAccount:
    def __init__(self, items):
        self.inbox = FakeFolder(items)


def make_mime(subject="Вопрос", body="Текст письма", message_id="<in@corp.ru>"):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Иван <ivan@company.ru>"
    msg["To"] = "llm@company.ru"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body, charset="utf-8")
    return msg.as_bytes()


def transport_with(items):
    transport = ews_client.EWSTransport()
    transport._account = FakeAccount(items)  # соединение не поднимаем
    return transport


def test_fetch_unseen_parses_mime():
    """Письмо забирается сырым MIME — дальше работает обычный парсер."""
    transport = transport_with([FakeItem(mime_content=make_mime(body="Привет из Exchange"))])

    fetched = transport.fetch_unseen()

    assert len(fetched) == 1
    handle, msg = fetched[0]
    # кириллица в заголовках приезжает как RFC 2047 — декодирует парсер
    assert decode_mime_header(msg["Subject"]) == "Вопрос"
    assert "Привет из Exchange" in msg.get_payload(decode=True).decode("utf-8")
    assert handle.is_read is False, "флаг ставится только после отправки ответа"


def test_items_without_mime_are_skipped():
    """Приглашения на встречи и прочие не-письма MIME не отдают — не падаем."""
    transport = transport_with([FakeItem(mime_content=None), FakeItem(mime_content=make_mime())])

    assert len(transport.fetch_unseen()) == 1


def test_mark_seen_saves_only_the_flag():
    item = FakeItem(mime_content=make_mime())
    transport = transport_with([item])

    transport.mark_seen(item)

    assert item.is_read is True
    assert item.saved_fields == ["is_read"], "лишние поля в UpdateItem — риск конфликта версий"


def test_unsee_returns_email_to_queue():
    item = FakeItem(mime_content=make_mime())
    item.is_read = True
    transport = transport_with([item])

    count = transport.unsee_by_message_id("<in@corp.ru>")

    assert count == 1
    assert item.is_read is False
    assert transport._folder().query.filters[-1] == {"message_id": "<in@corp.ru>"}


def test_send_reply_sends_raw_mime_and_returns_our_message_id(monkeypatch):
    """Ключевое свойство: Message-ID наш, а не присвоенный сервером.

    На нём держится сопоставление будущего Reply с сессией, поэтому письмо
    и уходит готовым MIME вместо сборки средствами EWS.
    """
    import exchangelib

    sent = {}

    class FakeEWSMessage:
        def __init__(self, account=None, mime_content=None):
            self.account = account
            self.mime_content = mime_content

        def send(self):
            sent["mime"] = self.mime_content
            sent["saved"] = False

        def send_and_save(self):
            sent["mime"] = self.mime_content
            sent["saved"] = True

    monkeypatch.setattr(exchangelib, "Message", FakeEWSMessage)
    transport = transport_with([])

    message_id = transport.send_reply(
        to_address="ivan@company.ru",
        subject="Вопрос",
        body="Ответ модели",
        session_title="Вопрос",
        in_reply_to="<in@corp.ru>",
        references=["<in@corp.ru>"],
    )

    assert sent, "письмо не отправлено"
    assert sent["saved"] is False, (
        "копия не должна оседать в «Отправленных» общего ящика: "
        "это архив ответов сразу всем пользователям сервиса"
    )
    parsed = email.message_from_bytes(sent["mime"])
    assert parsed["Message-ID"] == message_id
    assert parsed["In-Reply-To"] == "<in@corp.ru>"
    assert decode_mime_header(parsed["Subject"]) == "Re: Вопрос"
    assert parsed[LOOP_HEADER] == "1", "без метки петля не будет видна"
    assert REPLY_MARKER in parsed.get_payload(decode=True).decode("utf-8")


def test_auth_type_rejects_unknown_value(monkeypatch):
    monkeypatch.setattr(ews_client, "EWS_AUTH", "kerberos5")

    with pytest.raises(ValueError, match="EWS_AUTH"):
        ews_client._auth_type()


# --- выбор учётных данных -----------------------------------------------------


def _oauth2_env(monkeypatch, password=""):
    monkeypatch.setattr(ews_client, "EWS_AUTH", "oauth2")
    monkeypatch.setattr(ews_client, "EWS_CLIENT_ID", "app-id")
    monkeypatch.setattr(ews_client, "EWS_CLIENT_SECRET", "app-secret")
    monkeypatch.setattr(ews_client, "EWS_TENANT_ID", "tenant")
    monkeypatch.setattr(ews_client, "MAIL_PASSWORD", password)
    monkeypatch.setattr(ews_client, "MAIL_LOGIN", "svc-llm")
    monkeypatch.setattr(ews_client, "MAIL_ADDRESS", "llm@company.ru")


def test_oauth2_without_password_acts_as_application(monkeypatch):
    """Без пароля токен выдаётся приложению, и ящик указывается через Identity.

    Без Identity заголовок impersonation не собирается, и Exchange не поймёт,
    в чей ящик его пустили: у токена приложения контекста пользователя нет.
    """
    from exchangelib import OAuth2Credentials

    _oauth2_env(monkeypatch)

    credentials = ews_client._credentials()

    assert isinstance(credentials, OAuth2Credentials)
    assert credentials.identity.primary_smtp_address == "llm@company.ru"
    assert (credentials.client_id, credentials.tenant_id) == ("app-id", "tenant")


def test_oauth2_with_password_acts_as_user(monkeypatch):
    from exchangelib import OAuth2LegacyCredentials

    _oauth2_env(monkeypatch, password="secret")

    credentials = ews_client._credentials()

    assert isinstance(credentials, OAuth2LegacyCredentials)
    assert credentials.username == "svc-llm"
    assert credentials.client_id == "app-id"


def test_password_auth_stays_plain_credentials(monkeypatch):
    """NTLM и basic не должны затронуться появлением OAuth2."""
    from exchangelib import Credentials

    monkeypatch.setattr(ews_client, "EWS_AUTH", "ntlm")
    monkeypatch.setattr(ews_client, "MAIL_LOGIN", "CORP\\svc-llm")
    monkeypatch.setattr(ews_client, "MAIL_PASSWORD", "secret")

    credentials = ews_client._credentials()

    assert type(credentials) is Credentials
    assert credentials.username == "CORP\\svc-llm"


def test_kerberos_needs_no_credentials(monkeypatch):
    monkeypatch.setattr(ews_client, "EWS_AUTH", "gssapi")

    assert ews_client._credentials() is None
