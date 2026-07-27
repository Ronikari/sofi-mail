import pytest

from src import config, pipeline, storage


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Каждый тест работает на своей пустой базе."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()


@pytest.fixture(autouse=True)
def imap_transport(monkeypatch):
    """Тесты не должны зависеть от MAIL_TRANSPORT в .env разработчика.

    `config.load_dotenv()` подхватывает реальный .env, и при MAIL_TRANSPORT=ews
    вызов process_email без явного транспорта полез бы поднимать Exchange.
    """
    from src import transport

    monkeypatch.setattr(transport, "MAIL_TRANSPORT", "imap")


@pytest.fixture
def allow_sender(monkeypatch):
    """Разрешить адрес из фикстур и закрепить адрес самой модели."""
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["a.ludkov29@gmail.com"])
    monkeypatch.setattr(pipeline, "MAIL_ADDRESS", "llm.assistant@gmail.com")


@pytest.fixture
def fake_llm(monkeypatch):
    """Подмена генерации: тесты пайплайна не должны ждать модель."""
    from src import llm

    calls = []

    def generate(history, prompt):
        calls.append({"history": [dict(row) for row in history], "prompt": prompt})
        return f"ответ на: {prompt[:40]}"

    monkeypatch.setattr(llm, "generate", generate)
    return calls


@pytest.fixture
def sent_mail(monkeypatch):
    """Перехват отправки: возвращаем предсказуемый Message-ID."""
    from src import smtp_client

    outbox = []

    def send_reply(to_address, subject, body, session_title, in_reply_to=None, references=None):
        message_id = f"<sent-{len(outbox)}@llm>"
        outbox.append(
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

    monkeypatch.setattr(smtp_client, "send_reply", send_reply)
    return outbox
