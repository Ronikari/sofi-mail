# тесты транспорта EWS без обращения к Exchange.
# порядок: заглушки FakeAccount, FakeFolder и FakeQuery подставляются вместо
# соединения -> метод EWSTransport выполняется на них -> утверждение сверяет
# сырой MIME, заголовки треда либо тип учётных данных.
# вход: monkeypatch и MIME-сообщения, собранные функцией make_mime.
# выход: результат pytest; модуль пропускается целиком без exchangelib.
# проверяется ews_client.py; MIME ответа собирает reply_builder.py, константы
# LOOP_HEADER и REPLY_MARKER приходят из email_parser.py.
# запуск: pytest tests/test_ews_client.py
#
# предмет проверки — контракт транспорта: письмо уходит готовым MIME
# из reply_builder, поэтому заголовки треда и созданный проектом Message-ID
# доходят до Exchange неизменными

import email

import pytest

# модуль пропускается на машине без exchangelib: библиотека тянет requests,
# lxml и pyspnego, и в окружении разработчика она стоит не всегда
pytest.importorskip("exchangelib", reason="exchangelib не установлен")

from src import ews_client  # noqa: E402
from src.email_parser import LOOP_HEADER, REPLY_MARKER, decode_mime_header  # noqa: E402


# заглушка QuerySet exchangelib: методы filter, only и order_by возвращают
# сам объект, поэтому цепочка вызовов повторяет рабочий код
class FakeQuery:
    def __init__(self, items):
        self.items = items

        # filters копит переданные условия: тесты сверяют по ним запрос
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


# заглушка папки: счётчики total_count и unread_count читает метод describe
class FakeFolder:
    name = "Входящие"
    total_count = 3
    unread_count = 1

    def __init__(self, items):
        self.query = FakeQuery(items)

    def filter(self, **kwargs):
        return self.query.filter(**kwargs)


# заглушка письма exchangelib: saved_fields запоминает аргумент update_fields
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


# выход: байты MIME-сообщения для поля mime_content заглушки письма
def make_mime(subject="Вопрос", body="Текст письма", message_id="<in@corp.ru>"):
    """Собирает входящее письмо в виде сырого MIME."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "Иван <ivan@company.ru>"
    msg["To"] = "llm@company.ru"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body, charset="utf-8")
    return msg.as_bytes()


# выход: EWSTransport с подставленным соединением
def transport_with(items):
    """Создаёт транспорт с заглушкой соединения."""
    transport = ews_client.EWSTransport()
    transport._account = FakeAccount(items)  # соединение не поднимаем
    return transport


def test_fetch_unseen_parses_mime():
    """Письмо забирается сырым MIME и разбирается штатным парсером."""
    transport = transport_with([FakeItem(mime_content=make_mime(body="Привет из Exchange"))])

    fetched = transport.fetch_unseen()

    assert len(fetched) == 1
    handle, msg = fetched[0]
    # кириллица в заголовках приезжает как RFC 2047 — декодирует парсер
    assert decode_mime_header(msg["Subject"]) == "Вопрос"
    assert "Привет из Exchange" in msg.get_payload(decode=True).decode("utf-8")
    assert handle.is_read is False, "флаг ставится только после отправки ответа"


def test_items_without_mime_are_skipped():
    """Элементы папки без поля mime_content пропускаются."""
    # так приходят приглашения на встречи и прочие элементы, письмами
    # не являющиеся
    transport = transport_with([FakeItem(mime_content=None), FakeItem(mime_content=make_mime())])

    assert len(transport.fetch_unseen()) == 1


def test_mark_seen_saves_only_the_flag():
    """Отметка прочитанности отправляет на сервер одно поле."""
    item = FakeItem(mime_content=make_mime())
    transport = transport_with([item])

    transport.mark_seen(item)

    assert item.is_read is True
    assert item.saved_fields == ["is_read"], "лишние поля в UpdateItem — риск конфликта версий"


def test_unsee_returns_email_to_queue():
    """Снятие прочитанности идёт по фильтру message_id."""
    item = FakeItem(mime_content=make_mime())
    item.is_read = True
    transport = transport_with([item])

    count = transport.unsee_by_message_id("<in@corp.ru>")

    assert count == 1
    assert item.is_read is False

    # последний записанный фильтр показывает, по какому полю шёл поиск
    assert transport._folder().query.filters[-1] == {"message_id": "<in@corp.ru>"}


def test_send_reply_sends_raw_mime_and_returns_our_message_id(monkeypatch):
    """Отправленное письмо несёт Message-ID, созданный в reply_builder."""
    # на этом значении держится сопоставление будущего ответа с сессией,
    # поэтому письмо уходит готовым MIME
    import exchangelib

    sent = {}

    # заглушка письма exchangelib: запоминает переданный MIME и способ отправки
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

    # письмо разбирается обратно: проверяется то, что реально ушло на сервер
    parsed = email.message_from_bytes(sent["mime"])
    assert parsed["Message-ID"] == message_id
    assert parsed["In-Reply-To"] == "<in@corp.ru>"
    assert decode_mime_header(parsed["Subject"]) == "Re: Вопрос"
    assert parsed[LOOP_HEADER] == "1", "без метки петля не будет видна"
    assert REPLY_MARKER in parsed.get_payload(decode=True).decode("utf-8")


def test_auth_type_rejects_unknown_value(monkeypatch):
    """Неизвестное значение EWS_AUTH поднимает ошибку до запроса к серверу."""
    monkeypatch.setattr(ews_client, "EWS_AUTH", "kerberos5")

    with pytest.raises(ValueError, match="EWS_AUTH"):
        ews_client._auth_type()


# --- выбор учётных данных -----------------------------------------------------


# побочный эффект: подмена семи значений модуля ews_client.
# аргумент password переключает поток OAuth2: пустая строка даёт client
# credentials, непустая — ROPC
def _oauth2_env(monkeypatch, password=""):
    """Задаёт окружение режима EWS_AUTH=oauth2."""
    monkeypatch.setattr(ews_client, "EWS_AUTH", "oauth2")
    monkeypatch.setattr(ews_client, "EWS_CLIENT_ID", "app-id")
    monkeypatch.setattr(ews_client, "EWS_CLIENT_SECRET", "app-secret")
    monkeypatch.setattr(ews_client, "EWS_TENANT_ID", "tenant")
    monkeypatch.setattr(ews_client, "MAIL_PASSWORD", password)
    monkeypatch.setattr(ews_client, "MAIL_LOGIN", "svc-llm")
    monkeypatch.setattr(ews_client, "MAIL_ADDRESS", "llm@company.ru")


def test_oauth2_without_password_acts_as_application(monkeypatch):
    """Пустой пароль даёт учётные данные приложения с заданным Identity."""
    # Identity собирает заголовок impersonation: контекста пользователя
    # у токена приложения нет, и без него Exchange не определяет ящик
    from exchangelib import OAuth2Credentials

    _oauth2_env(monkeypatch)

    credentials = ews_client._credentials()

    assert isinstance(credentials, OAuth2Credentials)
    assert credentials.identity.primary_smtp_address == "llm@company.ru"
    assert (credentials.client_id, credentials.tenant_id) == ("app-id", "tenant")


def test_oauth2_with_password_acts_as_user(monkeypatch):
    """Непустой пароль даёт учётные данные потока ROPC."""
    from exchangelib import OAuth2LegacyCredentials

    _oauth2_env(monkeypatch, password="secret")

    credentials = ews_client._credentials()

    assert isinstance(credentials, OAuth2LegacyCredentials)
    assert credentials.username == "svc-llm"
    assert credentials.client_id == "app-id"


def test_password_auth_stays_plain_credentials(monkeypatch):
    """Режим ntlm даёт обычные учётные данные с логином и паролем."""
    from exchangelib import Credentials

    monkeypatch.setattr(ews_client, "EWS_AUTH", "ntlm")
    monkeypatch.setattr(ews_client, "MAIL_LOGIN", "CORP\\svc-llm")
    monkeypatch.setattr(ews_client, "MAIL_PASSWORD", "secret")

    credentials = ews_client._credentials()

    # сверка точным типом: классы OAuth2 наследуют Credentials, и isinstance
    # прошёл бы и на них
    assert type(credentials) is Credentials
    assert credentials.username == "CORP\\svc-llm"


def test_kerberos_needs_no_credentials(monkeypatch):
    """Режим gssapi работает без учётных данных."""
    # билет Kerberos берётся из кеша операционной системы
    monkeypatch.setattr(ews_client, "EWS_AUTH", "gssapi")

    assert ews_client._credentials() is None
