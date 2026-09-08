"""Путь вложения через пайплайн: загрузка, ссылка в запросе, follow-up, уборка.

Разбор документа выполняет Open WebUI, и здесь он заглушка: предмет проверки
в том, что делает пайплайн, а не в том, как отвечает чужой сервис. Файл уезжает
в хранилище байтами, обратно приходит идентификатор — проверяется, что уезжает
именно оригинал и что ссылка на него живёт по всему треду.
"""

from email.message import EmailMessage
from pathlib import Path

from src import attachment_context, pipeline, storage

FIXTURES = Path(__file__).parent / "fixtures"
SENDER = "a.ludkov29@gmail.com"


def mail(subject, message_id, body="Что в документе?", in_reply_to=None, attach=()):
    msg = EmailMessage()
    msg["From"] = f"Алексей <{SENDER}>"
    msg["To"] = "sofi@company.ru"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    for name, data in attach:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


def regulation():
    return ("регламент.pdf", (FIXTURES / "regulation_3pages.pdf").read_bytes())


# --- 1. Обычный путь --------------------------------------------------------


def test_attachment_is_uploaded_and_referenced(allow_sender, fake_llm, fake_owui_files, transport):
    """Оригинал файла уезжает в Open WebUI, а в запрос идёт ссылка на него."""
    original = regulation()[1]

    outcome = pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)

    assert outcome.status == "ok"
    name, data, content_type = fake_owui_files.uploaded[0]
    assert name == "регламент.pdf", "имя с расширением: по нему сервер выбирает парсер"
    assert data == original, "в хранилище уезжает оригинал, а не извлечённый текст"
    assert content_type == "application/pdf"
    assert fake_llm[0]["files"] == [{"type": "file", "id": "file-1"}], "режим подачи выбирает Open WebUI"


def test_document_description_goes_to_model_but_not_to_history(
    allow_sender, fake_llm, fake_owui_files, transport
):
    """В базе остаётся текст человека, описание документа живёт только в запросе."""
    pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)

    assert "приложен документ «регламент.pdf»" in fake_llm[0]["prompt"]
    stored = [row["body"] for row in storage.get_history(1, 10) if row["role"] == "user"]
    assert stored == ["Что в документе?"]


def test_file_id_is_stored_for_the_session(allow_sender, fake_llm, fake_owui_files, transport):
    size = len(regulation()[1])

    pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)

    rows = storage.get_session_files(1, 10)
    assert [(r["file_id"], r["filename"], r["bytes"]) for r in rows] == [("file-1", "регламент.pdf", size)]


def test_stored_size_survives_into_the_next_letter(
    allow_sender, fake_llm, fake_owui_files, transport
):
    """Вес документа кладётся в базу и описывает файл в следующих письмах.

    Пересобрать его следующее письмо треда не может: содержимого документа
    у нас нет, есть только строка в базе.
    """
    pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)

    stored = storage.get_session_files(1, 10)[0]
    assert stored["bytes"] > 0

    pipeline.process_email(
        mail("Re: Регламент", "<a2@mail>", body="А что в разделе 2?", in_reply_to="<a1@mail>"),
        transport=transport,
    )
    from src.attachments import size_words

    assert size_words(stored["bytes"]) in fake_llm[1]["prompt"]


def test_followup_reuses_the_file_without_uploading_again(
    allow_sender, fake_llm, fake_owui_files, transport
):
    """Второе письмо треда спрашивает про тот же документ, а не грузит копию.

    Ради этого file_id и лежит в базе: повторная загрузка удвоила бы то,
    что сервис оставил в общем хранилище сервисной учётной записи.
    """
    pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)
    pipeline.process_email(
        mail("Re: Регламент", "<a2@mail>", body="А что в разделе 2?", in_reply_to="<a1@mail>"),
        transport=transport,
    )

    assert len(fake_owui_files.uploaded) == 1, "документ загружен ровно один раз"
    assert fake_llm[1]["files"] == [{"type": "file", "id": "file-1"}]
    assert "Ранее в переписке приложен документ" in fake_llm[1]["prompt"]


def test_email_without_text_but_with_document_still_gets_an_answer(
    allow_sender, fake_llm, fake_owui_files, transport, sent_mail
):
    """«Смотри вложение» без единого слова — обычное письмо, а не пустое."""
    outcome = pipeline.process_email(
        mail("Регламент", "<a1@mail>", body="", attach=[regulation()]), transport=transport
    )

    assert outcome.status == "ok"
    assert "Коротко изложите суть документа" in fake_llm[0]["prompt"]
    assert len(sent_mail) == 1


def test_image_reaches_the_server_side_parser(
    allow_sender, fake_llm, fake_owui_files, transport
):
    """Скан больше не отсекается: распознавание текста есть на стороне Open WebUI."""
    pipeline.process_email(
        mail("Скан", "<a1@mail>", body="Что здесь?", attach=[("скан.png", b"\x89PNG\r\n\x1a\n")]),
        transport=transport,
    )

    assert fake_owui_files.uploaded[0][0] == "скан.png"
    assert fake_llm[0]["files"] == [{"type": "file", "id": "file-1"}]


# --- 2. Отказы не отменяют ответ -------------------------------------------


def test_unsupported_format_does_not_block_the_reply(
    allow_sender, fake_llm, fake_owui_files, transport, sent_mail
):
    """Ответ по тексту письма уходит, а про непринятый файл сказано прямо."""
    outcome = pipeline.process_email(
        mail("Архив", "<a1@mail>", body="Что скажешь?", attach=[("архив.zip", b"PK\x03\x04")]),
        transport=transport,
    )

    assert outcome.status == "ok"
    assert fake_owui_files.uploaded == []
    assert "не удалось приложить" in sent_mail[0]["body"]
    assert "архив.zip" in sent_mail[0]["body"]


def test_unavailable_file_service_is_reported_to_the_user(
    allow_sender, fake_llm, fake_owui_files, transport, sent_mail
):
    fake_owui_files.upload_error = RuntimeError("Open WebUI ответил 500")

    outcome = pipeline.process_email(
        mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport
    )

    assert outcome.status == "ok"
    assert "сервис документов недоступен" in sent_mail[0]["body"]


def test_document_only_email_with_refused_document_does_not_reach_the_model(
    allow_sender, fake_llm, fake_owui_files, transport, sent_mail
):
    """Ни вопроса, ни документа — спрашивать модель не о чем.

    Иначе на «коротко изложи, о чём документ» пришёл бы ответ по пустому месту,
    а причина отказа выглядела бы оговоркой к нему.
    """
    outcome = pipeline.process_email(
        mail("Архив", "<a1@mail>", body="", attach=[("архив.zip", b"PK\x03\x04")]),
        transport=transport,
    )

    assert outcome.status == "skipped"
    assert fake_llm == [], "запроса к модели не было"
    body = sent_mail[0]["body"]
    assert body.startswith("не удалось приложить"), "это письмо, а не приписка к ответу"
    assert "архив.zip" in body
    assert "Примечание" not in body


def test_attachments_can_be_switched_off(
    allow_sender, fake_llm, fake_owui_files, transport, sent_mail, monkeypatch
):
    monkeypatch.setattr(attachment_context, "ATTACHMENTS_ENABLED", False)

    pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)

    assert fake_owui_files.uploaded == []
    assert "отключена администратором" in sent_mail[0]["body"]


def test_extra_attachments_beyond_the_limit_are_reported(
    allow_sender, fake_llm, fake_owui_files, transport, sent_mail, monkeypatch
):
    monkeypatch.setattr(attachment_context, "ATTACHMENT_MAX_COUNT", 1)
    name, data = regulation()

    pipeline.process_email(
        mail("Пакет", "<a1@mail>", attach=[(name, data), ("второй.pdf", data)]), transport=transport
    )

    assert len(fake_owui_files.uploaded) == 1
    assert "не больше 1 файлов" in sent_mail[0]["body"]


# --- 2a. Старая база доживает до новой схемы --------------------------------


def test_old_database_gets_the_size_column(tmp_path, monkeypatch):
    """Колонку `bytes` доставляет миграция, и прежние строки её переживают.

    База живёт не в одном экземпляре: её разворачивают из бэкапа, копируют
    со стенда, заводят заново. `CREATE TABLE IF NOT EXISTS` по существующей
    таблице не делает ничего, поэтому колонку добавляет отдельный шаг —
    и он должен быть безобиден при каждом запуске, а не только при первом.
    Строки такой базы записаны прежней схемой, с колонками разбора.
    """
    import sqlite3

    # схема до перехода на серверный разбор: колонок разбора уже нет в SCHEMA,
    # поэтому старая таблица собирается здесь явно
    older = storage.SCHEMA.replace(
        "    bytes        INTEGER NOT NULL DEFAULT 0,\n",
        "    pages        INTEGER NOT NULL DEFAULT 0,\n"
        "    chars        INTEGER NOT NULL DEFAULT 0,\n"
        "    full_context INTEGER NOT NULL DEFAULT 0,\n"
        "    outline      TEXT,\n",
    )
    assert older != storage.SCHEMA, "схема изменилась — тест ловит не то, что должен"

    db_path = tmp_path / "старая.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(older)
    conn.execute(
        "INSERT INTO sessions(id, title, peer_email, created_at, updated_at) "
        "VALUES (1, 'Регламент', ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (SENDER,),
    )
    conn.execute(
        "INSERT INTO session_files"
        "(session_id, file_id, filename, pages, full_context, outline, message_id, created_at) "
        "VALUES (1, 'file-old', 'старый.pdf', 3, 1, '', '<old@mail>', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage, "DB_PATH", db_path)
    storage.init_db()
    storage.init_db()  # второй запуск демона на той же базе

    row = storage.get_session_files(1, 10)[0]
    assert row["filename"] == "старый.pdf", "прежние строки на месте"
    assert row["bytes"] == 0, "вес у них неизвестен — нулём, а не ошибкой"

    # вставка новой строки проходит по базе, где колонки разбора ещё на месте
    storage.add_session_file(1, "file-new", "новый.pdf", 9000, "<new@mail>")
    assert storage.get_session_files(1, 10)[0]["bytes"] == 9000


# --- 3. Примерка не оставляет следов ---------------------------------------


def test_dry_run_leaves_nothing_in_the_file_store(allow_sender, fake_llm, fake_owui_files, transport):
    """`once --dry-run` можно гонять сколько угодно: копий документа не остаётся."""
    pipeline.process_email(
        mail("Регламент", "<a1@mail>", attach=[regulation()]), dry_run=True, transport=transport
    )

    assert fake_owui_files.uploaded, "документ всё же загружался — иначе примерка ничего не проверяет"
    assert fake_owui_files.alive == set(), "и был удалён по итогам примерки"
    assert storage.list_session_files() == []


# --- 4. Повторная обработка и потолки --------------------------------------


def test_resent_after_delivery_failure_does_not_upload_twice(
    allow_sender, fake_llm, fake_owui_files, transport
):
    """Сбой отправки возвращает письмо в очередь — копии документа не будет.

    Письмо разбирается второй раз целиком, и без проверки по журналу файлов
    каждая неудачная отправка добавляла бы в чужое хранилище ещё один
    экземпляр того же документа.
    """
    letter = mail("Регламент", "<a1@mail>", attach=[regulation()])
    transport.send_error = RuntimeError("Exchange недоступен")
    assert pipeline.process_email(letter, transport=transport).status == "error"

    transport.send_error = None
    assert pipeline.process_email(letter, transport=transport).status == "ok"

    assert len(fake_owui_files.uploaded) == 1
    assert fake_llm[1]["files"] == [{"type": "file", "id": "file-1"}]


def test_thread_files_are_capped_for_the_whole_request(
    allow_sender, fake_llm, fake_owui_files, transport, monkeypatch
):
    """Потолок считается на весь запрос: новые файлы плюс прежние файлы треда."""
    monkeypatch.setattr(attachment_context, "ATTACHMENT_MAX_SESSION_FILES", 2)
    pipeline.process_email(mail("Регламент", "<a1@mail>", attach=[regulation()]), transport=transport)

    for n in (2, 3):
        storage.add_session_file(1, f"old-{n}", f"старый-{n}.pdf", 4096, f"<old{n}@mail>")

    pipeline.process_email(
        mail("Re: Регламент", "<a2@mail>", in_reply_to="<a1@mail>", attach=[regulation()]),
        transport=transport,
    )

    assert len(fake_llm[1]["files"]) == 2, "в запрос уехало больше файлов, чем разрешено"
