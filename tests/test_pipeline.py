"""Сопоставление сессий и обработка письма целиком (без сети)."""

import email
from email.message import EmailMessage

import pytest

from src import pipeline, storage

FROM = "Алексей <a.ludkov29@gmail.com>"
TO = "llm.assistant@gmail.com"


def make_email(subject, message_id, body="Вопрос?", in_reply_to=None, references=None, sender=FROM):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = TO
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Sat, 25 Jul 2026 19:12:03 +0300"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body, charset="utf-8")
    return email.message_from_bytes(msg.as_bytes())


# --- сопоставление сессий ---------------------------------------------------


def test_reply_continues_session(allow_sender, fake_llm, sent_mail):
    """Ответ на письмо модели должен попасть в ту же сессию."""
    pipeline.process_email(make_email("Вопрос про Python", "<u1@mail>"))
    reply_to = sent_mail[0]["message_id"]

    pipeline.process_email(
        make_email("Re: Вопрос про Python", "<u2@mail>", "А подробнее?", in_reply_to=reply_to)
    )

    assert len(storage.list_sessions()) == 1
    history = storage.get_history(1, 40)
    assert [row["role"] for row in history] == ["user", "assistant", "user", "assistant"]


def test_history_reaches_the_model(allow_sender, fake_llm, sent_mail):
    """Контекст прошлых реплик должен доехать до генерации, иначе чата нет."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    second_call = fake_llm[1]
    assert second_call["prompt"] == "Второй вопрос"
    assert [row["body"] for row in second_call["history"]] == [
        "Первый вопрос",
        "ответ на: Первый вопрос",
    ]


def test_new_subject_starts_new_session(allow_sender, fake_llm, sent_mail):
    pipeline.process_email(make_email("Первая тема", "<u1@mail>"))
    pipeline.process_email(make_email("Вторая тема", "<u2@mail>"))

    assert len(storage.list_sessions()) == 2
    assert fake_llm[1]["history"] == [], "новая тема не должна тянуть чужой контекст"


def test_session_found_via_references_when_in_reply_to_lost(allow_sender, fake_llm, sent_mail):
    """Часть клиентов теряет In-Reply-To, но сохраняет цепочку References."""
    pipeline.process_email(make_email("Тема", "<u1@mail>"))
    sent_id = sent_mail[0]["message_id"]

    pipeline.process_email(make_email("Re: Тема", "<u2@mail>", references=["<u1@mail>", sent_id]))

    assert len(storage.list_sessions()) == 1


def test_reply_keeps_thread_headers(allow_sender, fake_llm, sent_mail):
    """Без In-Reply-To/References ответ уедет в отдельный тред у получателя."""
    pipeline.process_email(make_email("Тема", "<u1@mail>"))
    assert sent_mail[0]["in_reply_to"] == "<u1@mail>"
    assert sent_mail[0]["to"] == "a.ludkov29@gmail.com"


# --- идемпотентность --------------------------------------------------------


def test_same_email_is_answered_once(allow_sender, fake_llm, sent_mail):
    msg = make_email("Тема", "<u1@mail>")
    first = pipeline.process_email(msg)
    second = pipeline.process_email(msg)

    assert first.status == "ok"
    assert second.status == "skipped"
    assert len(sent_mail) == 1


def test_smtp_failure_returns_email_to_queue(allow_sender, fake_llm, monkeypatch):
    """SMTP чаще всего лечится сам — письмо должно попасть в следующий проход."""
    from src import smtp_client

    def broken(*args, **kwargs):
        raise OSError("SMTP недоступен")

    monkeypatch.setattr(smtp_client, "send_reply", broken)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"))

    assert outcome.status == "error"
    # заявка снята: повторная обработка того же письма разрешена
    assert storage.claim_message("<u1@mail>") is True


def test_retry_after_smtp_failure_does_not_duplicate_question(allow_sender, fake_llm, monkeypatch):
    """Повторный проход не должен класть второй экземпляр вопроса в историю."""
    from src import smtp_client

    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)
    monkeypatch.setattr(smtp_client, "send_reply", lambda *a, **k: (_ for _ in ()).throw(OSError("нет сети")))
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    monkeypatch.setattr(smtp_client, "send_reply", lambda *a, **k: "<sent-0@llm>")
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    roles = [row["role"] for row in storage.get_history(1, 40)]
    assert roles == ["user", "assistant"]


def test_llm_failure_is_reported_and_recorded(allow_sender, sent_mail, monkeypatch):
    from src import llm

    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)
    monkeypatch.setattr(llm, "generate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama лежит")))

    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"))

    assert outcome.status == "error"
    assert not outcome.can_mark_seen, "письмо с ошибкой должно остаться видимым для retry"
    assert "ollama лежит" in sent_mail[0]["body"]
    assert [row["message_id"] for row in storage.list_failed()] == ["<u1@mail>"]


# --- фильтры ----------------------------------------------------------------


def test_stranger_is_ignored_silently(allow_sender, fake_llm, sent_mail):
    outcome = pipeline.process_email(make_email("Тема", "<x@mail>", sender="spam@evil.com"))

    assert outcome.status == "skipped"
    assert sent_mail == [], "посторонним не отвечаем: ответ подтвердил бы, что ящик живой"


def test_own_email_is_ignored(allow_sender, fake_llm, sent_mail):
    outcome = pipeline.process_email(make_email("Тема", "<x@mail>", sender=TO))

    assert outcome.status == "skipped"
    assert sent_mail == []


def test_rate_limit_stops_answering(allow_sender, fake_llm, sent_mail, monkeypatch):
    monkeypatch.setattr(pipeline, "RATE_LIMIT_PER_HOUR", 2)

    for i in range(4):
        pipeline.process_email(make_email(f"Тема {i}", f"<u{i}@mail>"))

    answers = [mail for mail in sent_mail if mail["body"].startswith("ответ на:")]
    assert len(answers) == 2
    assert "Превышен лимит" in sent_mail[2]["body"]


def test_long_email_is_truncated_not_rejected(allow_sender, fake_llm, sent_mail, monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_PROMPT_CHARS", 100)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "я" * 500))

    assert len(fake_llm[0]["prompt"]) == 100
    assert "обработано частично" in sent_mail[0]["body"]


def test_dry_run_has_no_side_effects(allow_sender, fake_llm, sent_mail):
    """«Примерка» должна быть повторяемой: ни письма, ни записей в базе."""
    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"), dry_run=True)

    assert outcome.status == "ok"
    assert sent_mail == []
    assert storage.list_sessions() == []
    # письмо не съедено: обычный запуск обработает его как новое
    assert storage.claim_message("<u1@mail>") is True


def test_tnef_email_gets_format_hint_not_empty_body_hint(fake_llm, sent_mail, monkeypatch):
    """Письмо Outlook в формате RTF: подсказка должна быть про формат письма.

    «В письме не нашлось текста» отправило бы пользователя искать ошибку
    не там — текст он написал, до нас он не дошёл из-за winmail.dat.
    """
    import email as email_module
    from pathlib import Path

    from src import config

    # заодно проверяется доменная запись whitelist: письмо из корпоративной сети
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    monkeypatch.setattr(pipeline, "MAIL_ADDRESS", "llm@company.ru")

    raw = (Path(__file__).parent / "fixtures" / "outlook_tnef.eml").read_bytes()
    outcome = pipeline.process_email(email_module.message_from_bytes(raw))

    assert outcome.status == "skipped"
    assert "winmail.dat" in sent_mail[0]["body"]
    assert fake_llm == [], "модель не должна вызываться для нечитаемого письма"
