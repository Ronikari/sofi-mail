"""Защита персональных данных: логи, объём хранимого, срок хранения, права.

Сервисом пользуются должностные лица компании, поэтому предмет этих тестов —
не работоспособность, а то, что переписка не расползается за пределы базы:
в лог, в лишние поля, в бессрочное хранение и в права доступа.
"""

import email
import logging
import stat
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from src import owui_files, pipeline, redact, storage


def make_email(subject, message_id, body="Вопрос?", sender="ceo@company.ru"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "llm@company.ru"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body, charset="utf-8")
    return email.message_from_bytes(msg.as_bytes())


# --- логи -------------------------------------------------------------------


def test_log_does_not_leak_subject_and_address(allow_domain, fake_llm, caplog, monkeypatch):
    """Тема письма и адрес не должны попадать в journald как есть."""
    monkeypatch.setattr(redact, "LOG_PII", False)

    with caplog.at_level(logging.DEBUG):
        pipeline.process_email(make_email("Сокращение отдела продаж", "<u1@company.ru>"))

    logged = caplog.text
    assert "Сокращение отдела продаж" not in logged, "тема письма попала в лог"
    assert "ceo@company.ru" not in logged, "полный адрес попал в лог"
    assert "c***@company.ru" in logged, "домен стоит оставить: по нему видно внешний отправитель или нет"


def test_log_pii_restores_full_detail(allow_domain, fake_llm, caplog, monkeypatch):
    """На время разбора инцидента полный лог должен возвращаться одним флагом."""
    monkeypatch.setattr(redact, "LOG_PII", True)

    with caplog.at_level(logging.DEBUG):
        pipeline.process_email(make_email("Сокращение отдела продаж", "<u1@company.ru>"))

    assert "Сокращение отдела продаж" in caplog.text
    assert "ceo@company.ru" in caplog.text


def test_masking_keeps_domain_but_hides_local_part():
    assert redact.email_addr("ivanov@company.ru") == "i***@company.ru"
    assert redact.email_addr("") == "?"
    assert redact.email_addr("мусор-без-собаки") == "***"


# --- объём хранимого --------------------------------------------------------


QUOTED = """Мой вопрос по проекту.

25.07.2026, 19:12, "Пётр" <petr@company.ru>:
> Внутренняя переписка третьих лиц, которую сервису никто не адресовал
"""


def test_quoted_correspondence_is_not_stored_by_default(allow_domain, fake_llm, monkeypatch):
    """В цитате едет переписка людей, которые сервису не писали."""
    monkeypatch.setattr(pipeline, "STORE_RAW_BODY", False)

    pipeline.process_email(make_email("Проект", "<u1@company.ru>", QUOTED))

    rows = storage.get_history(1, 40)
    assert rows[0]["body"] == "Мой вопрос по проекту."
    assert rows[0]["body_raw"] is None
    with storage.connect() as conn:
        dump = str(conn.execute("SELECT group_concat(body_raw) FROM messages").fetchone()[0])
    assert "petr@company.ru" not in dump


def test_raw_body_kept_when_explicitly_enabled(allow_domain, fake_llm, monkeypatch):
    """Отладочный режим должен оставаться доступным — но по явному решению."""
    monkeypatch.setattr(pipeline, "STORE_RAW_BODY", True)

    pipeline.process_email(make_email("Проект", "<u1@company.ru>", QUOTED))

    assert "petr@company.ru" in storage.get_history(1, 40)[0]["body_raw"]


# --- срок хранения ----------------------------------------------------------


def _age_session(session_id: int, days: int) -> None:
    """Состарить сессию, чтобы не ждать реального срока."""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with storage.connect() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))


def test_purge_removes_old_correspondence_and_keeps_fresh(allow_domain, fake_llm):
    pipeline.process_email(make_email("Старое", "<old@company.ru>"))
    pipeline.process_email(make_email("Свежее", "<new@company.ru>"))
    _age_session(1, days=200)

    sessions, _ = storage.purge_older_than(days=90)

    assert sessions == 1
    remaining = [row["title"] for row in storage.list_sessions()]
    assert remaining == ["Свежее"]


def test_purge_takes_replies_with_it(allow_domain, fake_llm):
    """Реплики должны уходить каскадом, иначе тексты писем переживут сессию."""
    pipeline.process_email(make_email("Старое", "<old@company.ru>", "Секретный текст"))
    _age_session(1, days=200)

    storage.purge_older_than(days=90)

    with storage.connect() as conn:
        left = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert left == 0, "переписка пережила свою сессию"


def test_retention_disabled_keeps_everything(allow_domain, fake_llm):
    """0 — прежнее поведение: площадка вправе хранить бессрочно осознанно."""
    pipeline.process_email(make_email("Старое", "<old@company.ru>"))
    _age_session(1, days=10000)

    assert storage.purge_older_than(days=0) == (0, 0)
    assert len(storage.list_sessions()) == 1


# --- удаление по требованию -------------------------------------------------


def test_forget_session_removes_it(allow_domain, fake_llm):
    pipeline.process_email(make_email("Тема", "<u1@company.ru>"))

    assert storage.delete_session(1) == 1
    assert storage.list_sessions() == []


def test_forget_address_removes_all_their_sessions(allow_domain, fake_llm):
    pipeline.process_email(make_email("Первая", "<u1@company.ru>", sender="ceo@company.ru"))
    pipeline.process_email(make_email("Вторая", "<u2@company.ru>", sender="ceo@company.ru"))
    pipeline.process_email(make_email("Чужая", "<u3@company.ru>", sender="cfo@company.ru"))

    assert storage.delete_sessions_by_address("CEO@Company.RU") == 2, "адрес нечувствителен к регистру"
    assert [row["peer_email"] for row in storage.list_sessions()] == ["cfo@company.ru"]


# --- документы в Open WebUI -------------------------------------------------
# Файл — единственное, что от переписки остаётся на чужой стороне: в базе лежит
# только ссылка. Поэтому оба способа удаления обязаны доставать и туда, иначе
# «удалили переписку» означало бы, что документы человека остались лежать
# в общем хранилище сервисной учётной записи.


def _age_file(file_id: str, days: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with storage.connect() as conn:
        conn.execute("UPDATE session_files SET created_at = ? WHERE file_id = ?", (stamp, file_id))


def _seed_file(session_id: int = 1, file_id: str = "file-1") -> None:
    storage.add_session_file(session_id, file_id, "регламент.pdf", 4096, "<m@mail>")


def test_expired_documents_are_deleted_from_open_webui(allow_domain, fake_llm, fake_owui_files):
    """Срок хранения файлов свой: в Open WebUI они лежат в более широком доступе."""
    pipeline.process_email(make_email("Тема", "<u1@company.ru>"))
    _seed_file()
    _age_file("file-1", days=45)

    removed, failed = owui_files.purge_expired(days=30)

    assert (removed, failed) == (1, 0)
    assert fake_owui_files.deleted == ["file-1"]
    assert storage.get_session_files(1, 10) == [], "удалённый файл не должен уезжать в запрос"


def test_fresh_documents_survive_the_purge(allow_domain, fake_llm, fake_owui_files):
    pipeline.process_email(make_email("Тема", "<u1@company.ru>"))
    _seed_file()

    assert owui_files.purge_expired(days=30) == (0, 0)
    assert fake_owui_files.deleted == []


def test_purging_correspondence_takes_its_documents_along(allow_domain, fake_llm, fake_owui_files):
    """Каскад унёс бы строки session_files — файлы остались бы в Open WebUI навсегда."""
    pipeline.process_email(make_email("Старое", "<old@company.ru>"))
    _seed_file()
    _age_session(1, days=200)

    files, sessions, _ = pipeline.run_retention()

    assert (files, sessions) == (1, 1)
    assert fake_owui_files.deleted == ["file-1"]


def test_forget_removes_documents_of_the_session(allow_domain, fake_llm, fake_owui_files):
    """Право на удаление данных — это и документы человека, а не только письма."""
    pipeline.process_email(make_email("Тема", "<u1@company.ru>"))
    _seed_file()

    owui_files.forget(storage.file_ids_of_sessions([1]))
    storage.delete_session(1)

    assert fake_owui_files.deleted == ["file-1"]
    assert storage.list_session_files() == []


def test_file_retention_can_be_disabled(allow_domain, fake_llm, fake_owui_files):
    """0 — файлы копятся осознанно, а не потому что уборка молча не сработала."""
    pipeline.process_email(make_email("Тема", "<u1@company.ru>"))
    _seed_file()
    _age_file("file-1", days=10000)

    assert owui_files.purge_expired(days=0) == (0, 0)
    assert fake_owui_files.deleted == []


# --- права на файлы ---------------------------------------------------------


@pytest.mark.parametrize("preset", [0o644, 0o666])
def test_database_is_not_readable_by_others(tmp_path, preset):
    """База с правами по умолчанию читается любым пользователем сервера."""
    db = tmp_path / "sub" / "sessions.db"
    storage.init_db(db)
    db.chmod(preset)
    (tmp_path / "sub").chmod(0o755)

    storage.init_db(db)  # любое обращение приводит права в порядок

    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700
