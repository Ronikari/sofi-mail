import itertools
import threading

import pytest

from src import config, pipeline, storage

# Message-ID отправленных писем должны быть уникальны в пределах всего прогона:
# messages.message_id — UNIQUE, и совпадение молча отбросит реплику (INSERT OR IGNORE).
# Счётчик общий на модуль, потому что в одном тесте бывает несколько транспортов.
_sent_counter = itertools.count()


class FakeTransport:
    """Транспорт-заглушка вместо Exchange.

    Реализует тот же контракт, что `src.transport.MailTransport`, поэтому
    тесты пайплайна проходят весь путь письма, ни разу не выходя в сеть.
    `send_error` позволяет изобразить недоступный Exchange, не подменяя методы
    по одному.
    """

    def __init__(self, emails=None):
        # дескриптор письма для EWS непрозрачен — здесь это просто индекс
        self.emails = list(enumerate(emails or []))
        self.sent = []
        self.seen = []
        self.send_error = None
        self._lock = threading.Lock()

    def fetch_unseen(self):
        return self.emails

    def mark_seen(self, handle):
        self.seen.append(handle)

    def unsee_by_message_id(self, message_id):
        return 1

    def send_reply(self, to_address, subject=None, body="", session_title="", in_reply_to=None, references=None):
        if self.send_error is not None:
            raise self.send_error
        with self._lock:
            message_id = f"<sent-{next(_sent_counter)}@llm>"
            self.sent.append(
                {
                    "to": to_address,
                    "subject": subject,
                    "body": body,
                    "title": session_title,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "message_id": message_id,
                }
            )
        return message_id

    def reconnect(self):
        pass

    def close(self):
        pass

    def describe(self):
        return "fake"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Каждый тест работает на своей пустой базе."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    """Подмена транспорта на заглушку — для всех тестов без исключения.

    `process_email` без явного транспорта зовёт `get_transport()`, а тот в этой
    сборке поднимает Exchange по .env разработчика. Автоподмена гарантирует,
    что забытый аргумент в тесте обернётся ошибкой ассерта, а не походом
    в корпоративную сеть.
    """
    from src import transport as transport_module

    fake = FakeTransport()
    # pipeline импортировал get_transport в свой модуль (`from ... import`),
    # поэтому патчить нужно обе ссылки, а не только оригинал
    monkeypatch.setattr(transport_module, "get_transport", lambda: fake)
    monkeypatch.setattr(pipeline, "get_transport", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def thread_matching(monkeypatch):
    """Склейка сессий по теме выключена — как в боевом умолчании .env.example.

    Значение config читает из .env разработчика на импорте, поэтому без явной
    фиксации набор пройденных веток зависел бы от чужого окружения: тесты
    проходили бы у одного и падали у другого. Тем, для кого склейка по теме и
    есть предмет проверки, её включает фикстура `thread_by_subject`.

    Патчится модуль pipeline, а не config: pipeline забрал значение к себе
    через `from ... import`, и подмена в config до него уже не доедет.
    """
    monkeypatch.setattr(pipeline, "THREAD_BY_SUBJECT", False)


@pytest.fixture
def thread_by_subject(monkeypatch):
    """Установка со склейкой по теме: письма одной темы продолжают одну сессию."""
    monkeypatch.setattr(pipeline, "THREAD_BY_SUBJECT", True)


@pytest.fixture
def sent_mail(transport):
    """Письма, ушедшие «в Exchange», в порядке отправки."""
    return transport.sent


@pytest.fixture
def allow_sender(monkeypatch):
    """Разрешить адрес из фикстур и закрепить адрес самой модели.

    Доменная запись здесь выключена намеренно: это боевое умолчание, и тесты
    должны идти по тому же пути, что и рабочая установка.
    """
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["a.ludkov29@gmail.com"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", False)
    monkeypatch.setattr(pipeline, "MAIL_ADDRESS", "llm.assistant@gmail.com")


@pytest.fixture
def allow_domain(monkeypatch):
    """Установка с доменным whitelist — для сценариев «любой сотрудник».

    Отдельная фикстура, а не аргумент к allow_sender: доменный доступ включается
    только там, где он и есть предмет теста.
    """
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", True)
    monkeypatch.setattr(pipeline, "MAIL_ADDRESS", "llm@company.ru")


@pytest.fixture
def fake_llm(monkeypatch):
    """Подмена генерации: тесты пайплайна не должны ждать модель."""
    from src import llm

    calls = []

    def generate(history, prompt, files=()):
        calls.append(
            {"history": [dict(row) for row in history], "prompt": prompt, "files": list(files)}
        )
        return f"ответ на: {prompt[:40]}"

    monkeypatch.setattr(llm, "generate", generate)
    return calls


class FakeOWUIFiles:
    """Заглушка файлового API Open WebUI: помнит, что загружено и что удалено."""

    def __init__(self) -> None:
        self.uploaded = []   # (имя, текст)
        self.deleted = []
        self.alive = set()
        self.upload_error = None
        self._counter = itertools.count(1)

    def upload(self, filename, text):
        if self.upload_error is not None:
            raise self.upload_error
        file_id = f"file-{next(self._counter)}"
        self.uploaded.append((filename, text))
        self.alive.add(file_id)
        return file_id

    def wait_processed(self, file_id, timeout=None):
        return None

    def delete(self, file_id):
        self.deleted.append(file_id)
        self.alive.discard(file_id)
        return True

    @staticmethod
    def reference(file_id, full_context):
        link = {"type": "file", "id": file_id}
        if full_context:
            link["context"] = "full"
        return link


@pytest.fixture
def fake_owui_files(monkeypatch):
    """Подмена файлового API: тесты вложений не должны выходить в сеть.

    Патчатся функции модуля, а не сам модуль: пайплайн импортирует
    `from src import owui_files` внутри функций, и подменённый атрибут
    модуля доезжает до всех вызовов.
    """
    from src import owui_files

    fake = FakeOWUIFiles()
    for name in ("upload", "wait_processed", "delete", "reference"):
        monkeypatch.setattr(owui_files, name, getattr(fake, name))
    return fake
