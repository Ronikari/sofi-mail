"""Разбор письма на .eml-фикстурах разных почтовых клиентов.

Сеть и БД здесь не нужны: email_parser — чистые функции над сообщением.
"""

import email
from pathlib import Path

import pytest

from src.email_parser import (
    LOOP_HEADER,
    NO_SUBJECT_TITLE,
    automated_reason,
    normalize_subject,
    parse_email,
    parse_message_ids,
    strip_quoted,
)

FIXTURES = Path(__file__).parent / "fixtures"
OWN_ADDRESS = "llm@company.ru"


def load(name: str):
    return email.message_from_bytes((FIXTURES / name).read_bytes())


# --- отсечение цитаты -------------------------------------------------------


@pytest.mark.parametrize(
    "fixture, expected_body",
    [
        ("gmail_ru_reply.eml", "А подробнее про второй пункт?"),
        ("yandex_reply.eml", "Спасибо, а как быть с ключом сортировки?"),
        ("outlook_reply.eml", "Давай пример кода."),
    ],
)
def test_quote_is_stripped(fixture, expected_body):
    assert parse_email(load(fixture)).body == expected_body


# единственная фикстура, у которой тела нет по существу: текст уехал в winmail.dat
EMPTY_BODY_BY_DESIGN = {"outlook_tnef.eml"}


def test_no_fixture_yields_empty_body():
    """Пустое тело — самый коварный баг: модель получает пустой промпт."""
    for path in FIXTURES.glob("*.eml"):
        if path.name in EMPTY_BODY_BY_DESIGN:
            continue
        assert parse_email(load(path.name)).body.strip(), f"пустое тело у {path.name}"


def test_our_own_signature_is_cut():
    """Маркер берётся из модуля: смена брендинга не должна ронять разбор."""
    from src.reply_builder import build_footer

    text = f"Вопрос пользователя.\n\n{build_footer('X')}"
    assert strip_quoted(text) == "Вопрос пользователя."


def test_plain_text_without_quotes_survives():
    text = "Первый абзац.\n\nВторой абзац — с дефисом в середине.\n"
    assert strip_quoted(text) == "Первый абзац.\n\nВторой абзац — с дефисом в середине."


def test_falls_back_to_raw_body_when_stripping_eats_everything():
    """Письмо, целиком похожее на цитату, лучше отдать как есть, чем не отдать."""
    raw = (
        "From: a@b.ru\r\nSubject: X\r\nMessage-ID: <1@b>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "> Только цитата и ничего больше\r\n"
    ).encode("utf-8")
    assert "Только цитата" in parse_email(email.message_from_bytes(raw)).body


# --- кодировки и заголовки --------------------------------------------------


def test_declared_cp1251_is_decoded():
    parsed = parse_email(load("cp1251.eml"))
    assert "windows-1251" in parsed.body
    assert parsed.body.startswith("Привет!")
    assert "=?" not in parsed.subject, "тема осталась в виде RFC 2047, а не текста"


def test_lying_charset_is_recovered():
    """Заявлен utf-8, внутри cp1251 — типично для старых российских клиентов."""
    assert parse_email(load("false_charset.eml")).body == "Кодировка заявлена неверно."


def test_html_only_message_loses_quote_block():
    parsed = parse_email(load("html_only.eml"))
    assert "Как отсортировать список?" in parsed.body
    assert "Важно" in parsed.body
    assert "цитата" not in parsed.body


def test_missing_message_id_gets_stable_surrogate():
    first = parse_email(load("no_message_id.eml")).message_id
    second = parse_email(load("no_message_id.eml")).message_id
    assert first == second, "суррогат должен быть детерминированным"
    assert first.startswith("<synthetic-")


# --- нормализация темы ------------------------------------------------------


@pytest.mark.parametrize(
    "subject, expected",
    [
        ("Re: Вопрос про Python", "Вопрос про Python"),
        ("RE: FWD: Вопрос про Python", "Вопрос про Python"),
        ("Re[2]: Вопрос про Python", "Вопрос про Python"),
        ("Ответ: Вопрос  про   Python", "Вопрос про Python"),
        ("ПЕР: Вопрос про Python", "Вопрос про Python"),  # пересылка в русском Outlook
        ("Вопрос про Python", "Вопрос про Python"),
        ("Перенос сроков", "Перенос сроков"),  # "Пер" внутри слова не префикс
        ("", NO_SUBJECT_TITLE),
    ],
)
def test_subject_normalization(subject, expected):
    assert normalize_subject(subject) == expected


def test_reply_and_original_share_one_session_title():
    """Тема — ключ фоллбэка при поиске сессии, поэтому обе формы должны совпасть."""
    assert normalize_subject("Re: Вопрос про Python") == normalize_subject("Вопрос про Python")


# --- заголовки треда --------------------------------------------------------


def test_ancestors_are_ordered_from_nearest():
    parsed = parse_email(load("gmail_ru_reply.eml"))
    assert parsed.ancestor_ids[0] == "<reply-1@gmail.com>"
    assert "<orig-1@gmail.com>" in parsed.ancestor_ids


def test_references_parsing_handles_line_folding():
    assert parse_message_ids("<a@x>\r\n <b@x>\t<c@x>") == ["<a@x>", "<b@x>", "<c@x>"]


# --- фильтры петель ---------------------------------------------------------


def test_autoreply_is_rejected():
    assert automated_reason(load("autoreply.eml"), OWN_ADDRESS) is not None


def test_own_message_is_rejected():
    msg = email.message_from_string(f"From: {OWN_ADDRESS}\nSubject: X\n\nтекст\n")
    assert automated_reason(msg, OWN_ADDRESS) == "письмо от самого себя"


def test_loop_header_is_rejected():
    msg = email.message_from_string(f"From: a@b.ru\n{LOOP_HEADER}: 1\nSubject: X\n\nтекст\n")
    assert automated_reason(msg, OWN_ADDRESS) is not None


def test_noreply_sender_is_rejected():
    msg = email.message_from_string("From: no-reply@service.com\nSubject: X\n\nтекст\n")
    assert automated_reason(msg, OWN_ADDRESS) is not None


def test_normal_message_passes():
    assert automated_reason(load("gmail_ru_reply.eml"), OWN_ADDRESS) is None


# --- Exchange и Outlook -----------------------------------------------------


def test_tnef_body_is_recognised_not_treated_as_empty():
    """Письмо Outlook в формате RTF: тело в winmail.dat.

    Отличить его от «пользователь прислал пустое письмо» обязательно —
    подсказки в ответе нужны разные.
    """
    parsed = parse_email(load("outlook_tnef.eml"))

    assert parsed.body == ""
    assert parsed.is_tnef is True


def test_plain_empty_email_is_not_marked_as_tnef():
    msg = email.message_from_string("From: a@b.ru\nSubject: X\n\n\n")
    assert parse_email(msg).is_tnef is False
