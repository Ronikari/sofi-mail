# общие фикстуры и заглушки для тестов проекта.
# порядок: автофикстуры поднимают пустую базу, подменяют транспорт и фиксируют
# настройку склейки сессий -> тест берёт нужные ему фикстуры аргументами.
# вход: временный каталог pytest (tmp_path) и monkeypatch.
# выход: заглушки FakeTransport и FakeOWUIFiles, списки отправленных писем
# и вызовов модели.
# подменяются storage.DB_PATH, transport.get_transport, pipeline.get_transport,
# pipeline.THREAD_BY_SUBJECT, summarizer.MAIL_DISPLAY_NAME,
# reply_builder.MAIL_DISPLAY_NAME, config.ALLOWED_SENDERS, llm.generate
# и функции owui_files.
# файл читают все модули tests/: test_pipeline.py, test_concurrency.py,
# test_email_parser.py, test_config.py, test_ews_client.py.
# сеть в тестах не используется: транспорт и файловый api заменены заглушками

import itertools
import threading

import pytest

from src import config, pipeline, storage

# Message-ID отправленных писем должны быть уникальны в пределах всего прогона:
# messages.message_id — UNIQUE, и совпадение молча отбросит реплику (INSERT OR IGNORE).
# Счётчик общий на модуль, потому что в одном тесте бывает несколько транспортов.
_sent_counter = itertools.count()


# заглушка Exchange: повторяет контракт src.transport.MailTransport, поэтому
# тесты проходят весь путь письма без выхода в сеть
class FakeTransport:
    def __init__(self, emails=None):
        # дескриптор письма для EWS непрозрачен — здесь это просто индекс
        self.emails = list(enumerate(emails or []))

        # sent накапливает отправленные письма, seen — дескрипторы, помеченные
        # прочитанными
        self.sent = []
        self.seen = []

        # число вызовов mark_seen_bulk: по нему тесты отличают один запрос
        # на пачку от запроса на каждое письмо
        self.bulk_calls = 0

        # присвоенное исключение изображает недоступный Exchange без подмены
        # отдельных методов
        self.send_error = None

        # замок на список sent: run_once обрабатывает письма пулом потоков
        self._lock = threading.Lock()

    def fetch_unseen(self):
        return self.emails

    def mark_seen(self, handle):
        self.seen.append(handle)

    def mark_seen_bulk(self, handles):
        # заглушка складывает дескрипторы в тот же список и считает вызовы
        self.bulk_calls += 1
        self.seen.extend(handles)

    def unsee_by_message_id(self, message_id):
        # возврат 1 означает найденное в папке письмо: команда retry получает
        # разрешение чистить журнал
        return 1

    def send_reply(
        self, to_address, subject=None, body="", session_title="", in_reply_to=None,
        references=None, thread_index="", incoming_topic="",
        sender_name="", quoted_body="", sent_date="",
    ):
        if self.send_error is not None:
            raise self.send_error

        with self._lock:
            # счётчик общий на модуль: колонка messages.message_id объявлена UNIQUE
            message_id = f"<sent-{next(_sent_counter)}@llm>"
            self.sent.append(
                {
                    "to": to_address,
                    "subject": subject,
                    "body": body,
                    "title": session_title,
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "thread_index": thread_index,
                    "incoming_topic": incoming_topic,
                    "sender_name": sender_name,
                    "quoted_body": quoted_body,
                    "sent_date": sent_date,
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


# побочный эффект: подмена storage.DB_PATH и создание таблиц во временном файле.
# автофикстура: каждый тест получает свою пустую базу
@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Поднимает пустую базу во временном каталоге теста."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()


# выход: объект FakeTransport, общий для теста.
# автофикстура: process_email без явного транспорта вызывает get_transport,
# а тот поднимает Exchange по .env разработчика. подмена превращает забытый
# аргумент в ошибку утверждения теста
@pytest.fixture(autouse=True)
def transport(monkeypatch):
    """Подменяет почтовый транспорт заглушкой во всех тестах."""
    from src import transport as transport_module

    fake = FakeTransport()

    # pipeline импортировал get_transport в свой модуль (`from ... import`),
    # поэтому патчатся обе ссылки
    monkeypatch.setattr(transport_module, "get_transport", lambda: fake)
    monkeypatch.setattr(pipeline, "get_transport", lambda: fake)
    return fake


# побочный эффект: pipeline.THREAD_BY_SUBJECT переводится в False.
# автофикстура: значение config читается из .env разработчика на импорте,
# и без фиксации набор пройденных веток зависел бы от чужого окружения.
# тестам, где склейка по теме и есть предмет проверки, её включает фикстура
# thread_by_subject
@pytest.fixture(autouse=True)
def thread_matching(monkeypatch):
    """Выключает склейку сессий по теме, повторяя умолчание .env.example."""
    # патчится модуль pipeline: он забрал значение к себе через `from ... import`,
    # и подмена в config до него не доходит
    monkeypatch.setattr(pipeline, "THREAD_BY_SUBJECT", False)


# побочный эффект: pipeline.THREAD_BY_SUBJECT переводится в True
@pytest.fixture
def thread_by_subject(monkeypatch):
    """Включает склейку сессий по теме письма."""
    monkeypatch.setattr(pipeline, "THREAD_BY_SUBJECT", True)


# побочный эффект: MAIL_DISPLAY_NAME фиксируется значением "Sofi" в модулях,
# забравших его к себе через `from ... import` (config.py: значение читается
# из .env на импорте, подмена в config после импорта модулей не доходит).
# автофикстура: без неё summarizer.py собирает подписи реплик по умолчанию
# config.py ("Local LLM") в окружении без .env разработчика (чистый checkout,
# стадия test в Dockerfile) и по значению из .env.example ("Sofi") на машине
# с настроенным .env — набор пройденных веток и ожидания тестов зависели бы
# от чужого окружения, тот же класс проблемы, что у thread_matching
@pytest.fixture(autouse=True)
def mail_display_name(monkeypatch):
    """Фиксирует MAIL_DISPLAY_NAME значением "Sofi" независимо от .env."""
    from src import reply_builder, summarizer

    monkeypatch.setattr(summarizer, "MAIL_DISPLAY_NAME", "Sofi")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")


# выход: список отправленных писем заглушки в порядке отправки
@pytest.fixture
def sent_mail(transport):
    """Отдаёт письма, ушедшие через заглушку транспорта."""
    return transport.sent


# побочный эффект: подмена ALLOWED_SENDERS, ALLOW_DOMAIN_WILDCARD и адреса ящика.
# доменная запись здесь выключена: это боевое умолчание, и тесты идут по тому
# же пути, что рабочая установка
@pytest.fixture
def allow_sender(monkeypatch):
    """Разрешает адрес из фикстур и закрепляет адрес ящика модели."""
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["a.ludkov29@gmail.com"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", False)
    monkeypatch.setattr(pipeline, "MAIL_ADDRESS", "llm.assistant@gmail.com")


# побочный эффект тот же, что у allow_sender, с доменной записью в whitelist.
# отдельная фикстура: доменный доступ включается в тестах, где он и есть
# предмет проверки
@pytest.fixture
def allow_domain(monkeypatch):
    """Открывает доступ всему домену для сценариев «любой сотрудник»."""
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", True)
    monkeypatch.setattr(pipeline, "MAIL_ADDRESS", "llm@company.ru")


# выход: список вызовов подменённой llm.generate; каждый элемент хранит
# историю, текст запроса и ссылки на файлы.
# побочный эффект: подмена llm.generate заглушкой
@pytest.fixture
def fake_llm(monkeypatch):
    """Подменяет генерацию ответа, освобождая тесты от ожидания модели."""
    from src import llm

    calls = []

    def generate(history, prompt, files=()):
        # строки истории копируются в словари: объекты sqlite3.Row живут
        # до закрытия соединения
        calls.append(
            {"history": [dict(row) for row in history], "prompt": prompt, "files": list(files)}
        )
        return f"ответ на: {prompt[:40]}"

    monkeypatch.setattr(llm, "generate", generate)
    return calls


# заглушка файлового api Open WebUI: запоминает загруженные и удалённые файлы
class FakeOWUIFiles:
    def __init__(self) -> None:
        self.uploaded = []   # (имя, байты, mime-тип)
        self.deleted = []

        # alive хранит идентификаторы файлов, оставшихся в хранилище
        self.alive = set()

        # присвоенное исключение изображает отказ загрузки
        self.upload_error = None
        self._counter = itertools.count(1)

    def upload(self, filename, data, content_type="application/octet-stream"):
        if self.upload_error is not None:
            raise self.upload_error

        file_id = f"file-{next(self._counter)}"
        self.uploaded.append((filename, data, content_type))
        self.alive.add(file_id)
        return file_id

    def wait_processed(self, file_id, timeout=None):
        # заглушка отдаёт готовность сразу: ожидание обработки в тестах
        # не проверяется
        return None

    def delete(self, file_id, *, force=False):
        self.deleted.append(file_id)

        # discard гасит повторное удаление того же идентификатора
        self.alive.discard(file_id)
        return True

    @staticmethod
    def reference(file_id):
        # форма ссылки повторяет owui_files.reference: тесты сверяют её
        # с содержимым запроса. поля context здесь нет — режим подачи
        # документа выбирает Open WebUI
        return {"type": "file", "id": file_id}


# выход: объект FakeOWUIFiles.
# побочный эффект: подмена функций upload, wait_processed, delete и reference
# в модуле owui_files
@pytest.fixture
def fake_owui_files(monkeypatch):
    """Подменяет файловый api Open WebUI, освобождая тесты от сети."""
    from src import owui_files

    fake = FakeOWUIFiles()

    # патчатся функции модуля: pipeline выполняет `from src import owui_files`
    # внутри функций, и подменённые атрибуты доезжают до всех вызовов
    for name in ("upload", "wait_processed", "delete", "reference"):
        monkeypatch.setattr(owui_files, name, getattr(fake, name))
    return fake
