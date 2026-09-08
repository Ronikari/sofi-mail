# тесты разбора письма на .eml-файлах разных почтовых клиентов.
# порядок: load читает файл фикстуры -> функция email_parser разбирает его ->
# утверждение сверяет тело, тему, заголовки треда либо причину отказа.
# вход: .eml-файлы из tests/fixtures.
# выход: результат pytest.
# проверяется email_parser.py; функция build_footer берётся из reply_builder.py.
# сеть и база здесь не нужны: email_parser состоит из чистых функций
# над объектом сообщения.
# запуск: pytest tests/test_email_parser.py

import email
from pathlib import Path

import pytest

from src.email_parser import (
    LOOP_HEADER,
    NO_SUBJECT_TITLE,
    REPLY_MARKER,
    automated_reason,
    is_forwarded,
    normalize_subject,
    parse_email,
    parse_message_ids,
    strip_quoted,
)

FIXTURES = Path(__file__).parent / "fixtures"
OWN_ADDRESS = "llm@company.ru"


# вход: имя файла в tests/fixtures.
# выход: объект email.message.Message
def load(name: str):
    """Читает .eml-файл фикстуры в объект сообщения."""
    return email.message_from_bytes((FIXTURES / name).read_bytes())


# --- отсечение цитаты -------------------------------------------------------


# набор покрывает три формы строки атрибуции: Gmail в русской локали,
# Яндекс и Outlook
@pytest.mark.parametrize(
    "fixture, expected_body",
    [
        ("gmail_ru_reply.eml", "А подробнее про второй пункт?"),
        ("yandex_reply.eml", "Спасибо, а как быть с ключом сортировки?"),
        ("outlook_reply.eml", "Давай пример кода."),
    ],
)
def test_quote_is_stripped(fixture, expected_body):
    """Цитата предыдущего письма отрезается у трёх почтовых клиентов."""
    assert parse_email(load(fixture)).body == expected_body


# единственная фикстура, у которой тела нет по существу: текст уехал в winmail.dat
EMPTY_BODY_BY_DESIGN = {"outlook_tnef.eml"}


def test_no_fixture_yields_empty_body():
    """Ни одна фикстура не даёт пустого тела после разбора."""
    # пустое тело доходит до модели пустым запросом, и ответ по нему
    # по виду совпадает с ответом на вопрос
    for path in FIXTURES.glob("*.eml"):
        if path.name in EMPTY_BODY_BY_DESIGN:
            continue
        assert parse_email(load(path.name)).body.strip(), f"пустое тело у {path.name}"


def test_our_own_signature_is_cut():
    """Подпись исходящего письма отрезается вместе с цитатой."""
    # подпись строится тем же build_footer, что и в рабочей отправке: смена
    # текста подписи разбор не ломает
    from src.reply_builder import build_footer

    text = f"Вопрос пользователя.\n\n{build_footer('X')}"
    assert strip_quoted(text) == "Вопрос пользователя."


def test_plain_text_without_quotes_survives():
    """Текст без цитаты доходит до модели целиком."""
    # дефис в середине строки не совпадает с разделителем подписи по RFC 3676
    text = "Первый абзац.\n\nВторой абзац — с дефисом в середине.\n"
    assert strip_quoted(text) == "Первый абзац.\n\nВторой абзац — с дефисом в середине."


def test_falls_back_to_raw_body_when_stripping_eats_everything():
    """Письмо из одной цитаты отдаётся телом без обработки."""
    raw = (
        "From: a@b.ru\r\nSubject: X\r\nMessage-ID: <1@b>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "> Только цитата и ничего больше\r\n"
    ).encode("utf-8")
    assert "Только цитата" in parse_email(email.message_from_bytes(raw)).body


# --- кодировки и заголовки --------------------------------------------------


def test_declared_cp1251_is_decoded():
    """Тело и тема письма в cp1251 приходят текстом."""
    parsed = parse_email(load("cp1251.eml"))
    assert "windows-1251" in parsed.body
    assert parsed.body.startswith("Привет!")
    assert "=?" not in parsed.subject, "тема осталась в виде RFC 2047, а не текста"


def test_lying_charset_is_recovered():
    """Тело с неверно заявленным charset разбирается перебором кодировок."""
    # заявлен utf-8, внутри cp1251: типичное поведение старых российских клиентов
    assert parse_email(load("false_charset.eml")).body == "Кодировка заявлена неверно."


def test_html_only_message_loses_quote_block():
    """Из html-письма извлекается текст без блока цитаты."""
    parsed = parse_email(load("html_only.eml"))
    assert "Как отсортировать список?" in parsed.body
    assert "Важно" in parsed.body
    assert "цитата" not in parsed.body


def test_missing_message_id_gets_stable_surrogate():
    """Письмо без Message-ID получает одинаковый суррогат при каждом разборе."""
    # журнал обработки использует это значение ключом, поэтому повторное чтение
    # папки должно давать тот же идентификатор
    first = parse_email(load("no_message_id.eml")).message_id
    second = parse_email(load("no_message_id.eml")).message_id
    assert first == second, "суррогат должен быть детерминированным"
    assert first.startswith("<synthetic-")


# --- нормализация темы ------------------------------------------------------


# набор покрывает каскад префиксов, счётчик Re[N], русские формы и слово,
# начинающееся с тех же букв
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
    """Префиксы ответа и пересылки снимаются с темы письма."""
    assert normalize_subject(subject) == expected


def test_reply_and_original_share_one_session_title():
    """Тема ответа и тема исходного письма дают одно название сессии."""
    # название сессии служит ключом поиска по теме в storage.find_session_by_subject
    assert normalize_subject("Re: Вопрос про Python") == normalize_subject("Вопрос про Python")


# --- заголовки треда --------------------------------------------------------


def test_ancestors_are_ordered_from_nearest():
    """Предки письма идут от ближайшего к дальнему."""
    # порядок задаёт приоритет поиска сессии в storage.find_session_by_message_ids
    parsed = parse_email(load("gmail_ru_reply.eml"))
    assert parsed.ancestor_ids[0] == "<reply-1@gmail.com>"
    assert "<orig-1@gmail.com>" in parsed.ancestor_ids


def test_references_parsing_handles_line_folding():
    """Цепочка References разбирается через переносы строк и табуляции."""
    # длинный заголовок переносится по RFC 5322 с отступом в начале строки
    assert parse_message_ids("<a@x>\r\n <b@x>\t<c@x>") == ["<a@x>", "<b@x>", "<c@x>"]


# --- фильтры петель ---------------------------------------------------------


def test_autoreply_is_rejected():
    """Письмо автоответчика остаётся без обработки."""
    assert automated_reason(load("autoreply.eml"), OWN_ADDRESS) is not None


def test_own_message_is_rejected():
    """Письмо с адреса самого ящика опознаётся по полю From."""
    msg = email.message_from_string(f"From: {OWN_ADDRESS}\nSubject: X\n\nтекст\n")
    assert automated_reason(msg, OWN_ADDRESS) == "письмо от самого себя"


def test_loop_header_is_rejected():
    """Письмо с собственным заголовком проекта опознаётся как петля."""
    msg = email.message_from_string(f"From: a@b.ru\n{LOOP_HEADER}: 1\nSubject: X\n\nтекст\n")
    assert automated_reason(msg, OWN_ADDRESS) is not None


def test_noreply_sender_is_rejected():
    """Адрес вида no-reply опознаётся по локальной части."""
    msg = email.message_from_string("From: no-reply@service.com\nSubject: X\n\nтекст\n")
    assert automated_reason(msg, OWN_ADDRESS) is not None


def test_normal_message_passes():
    """Письмо человека проходит фильтры петель."""
    assert automated_reason(load("gmail_ru_reply.eml"), OWN_ADDRESS) is None


# --- Exchange и Outlook -----------------------------------------------------


def test_tnef_body_is_recognised_not_treated_as_empty():
    """Письмо в формате RTF помечается признаком is_tnef."""
    # признак отделяет такое письмо от письма с пустым телом: подсказки
    # пользователю по ним различаются
    parsed = parse_email(load("outlook_tnef.eml"))

    assert parsed.body == ""
    assert parsed.is_tnef is True


def test_plain_empty_email_is_not_marked_as_tnef():
    """Обычное пустое письмо признака is_tnef не получает."""
    msg = email.message_from_string("From: a@b.ru\nSubject: X\n\n\n")
    assert parse_email(msg).is_tnef is False


# --- пересылка --------------------------------------------------------------


# набор покрывает префиксы темы четырёх клиентов и слово, начинающееся
# с тех же букв
@pytest.mark.parametrize(
    "subject, expected",
    [
        ("Fwd: Кадры", True),
        ("FW: Кадры", True),
        ("ПЕР: Кадры", True),
        ("Пересылаемое сообщение: Кадры", True),
        ("Re: Кадры", False),
        ("Кадры", False),
        ("Перенос сроков", False),
    ],
)
def test_forward_is_detected_by_subject(subject, expected):
    """Префикс темы опознаёт пересланное письмо."""
    assert is_forwarded(subject, "текст письма") is expected


# набор покрывает разделители, которыми клиенты открывают пересланное письмо
@pytest.mark.parametrize(
    "body",
    [
        "---------- Forwarded message ---------\nОт: boss@company.ru",
        "-------- Пересылаемое сообщение --------\nОт: boss@company.ru",
        '<div id="divRplyFwdMsg">От: boss@company.ru',
    ],
)
def test_forward_is_detected_by_body(body):
    """Разделитель в теле опознаёт пересылку без префикса в теме."""
    # префикс темы правят вручную, разделитель в теле остаётся
    assert is_forwarded("Кадры", body) is True


def test_forwarded_email_restores_body_without_headers():
    """Пересылка без слов от себя восстанавливает тело без шапки цитаты."""
    # шапка «От:/Кому:/Тема:» вырезается, остальной текст пересылки остаётся:
    # его отправитель не написал сам, но он же и не входит ни в одну сессию
    # этого отправителя, дублирования истории здесь нет
    raw = (
        "From: a@b.ru\r\nSubject: Fwd: Кадры\r\nMessage-ID: <f1@b>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "От: boss@company.ru\r\nКому: dept@company.ru\r\nТема: Кадры\r\n\r\n"
        "Готовим сокращение отдела продаж\r\n"
    ).encode("utf-8")

    parsed = parse_email(email.message_from_bytes(raw))

    assert parsed.is_forward is True
    assert "сокращение" in parsed.body


def test_forwarded_conversation_with_model_keeps_own_replies():
    """Пересылка переписки с моделью сохраняет её реплики как контекст."""
    # второй пользователь получил переписку с моделью пересылкой и переслал
    # её в ящик модели: для его сессии эта переписка не лежит в истории,
    # поэтому маркер REPLY_MARKER внутри пересланного текста не должен
    # обрезать тело — strip_own_replies здесь не применяется
    raw = (
        "From: c@b.ru\r\nSubject: Fwd: Кадры\r\nMessage-ID: <f2@b>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "---------- Forwarded message ---------\r\n"
        "От: boss@company.ru\r\nКому: dept@company.ru\r\nТема: Кадры\r\n\r\n"
        "Готовим сокращение отдела продаж\r\n\r\n"
        f"{REPLY_MARKER} Сокращение затронет три позиции.\r\n"
        f"{REPLY_MARKER} · сессия «Кадры»\r\n"
    ).encode("utf-8")

    parsed = parse_email(email.message_from_bytes(raw))

    assert parsed.is_forward is True
    assert "сокращение" in parsed.body
    assert "Сокращение затронет три позиции" in parsed.body


def test_own_question_before_forward_keeps_thread_below_it():
    """Вопрос над пересылкой не отрезает сам пересланный тред."""
    # второй пользователь спрашивает про пересланную переписку своими словами
    # прямо над разделителем "---------- Forwarded message ---------": старая
    # эвристика видела в разделителе границу цитаты и отбрасывала весь тред
    # ниже неё, оставляя модели только вопрос без предмета
    raw = (
        "From: c@b.ru\r\nSubject: Fwd: Кадры\r\nMessage-ID: <f3@b>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "Расскажи, что в этой переписке\r\n\r\n"
        "---------- Forwarded message ---------\r\n"
        "От: boss@company.ru\r\nКому: dept@company.ru\r\nТема: Кадры\r\n\r\n"
        "Готовим сокращение отдела продаж\r\n\r\n"
        f"{REPLY_MARKER} Сокращение затронет три позиции.\r\n"
        f"{REPLY_MARKER} · сессия «Кадры»\r\n"
    ).encode("utf-8")

    parsed = parse_email(email.message_from_bytes(raw))

    assert parsed.is_forward is True
    assert "Расскажи, что в этой переписке" in parsed.body
    assert "сокращение" in parsed.body
    assert "Сокращение затронет три позиции" in parsed.body


def test_quote_only_email_still_falls_back_to_raw_body():
    """Письмо без признаков пересылки восстанавливает тело без шапок."""
    # тот же текст без префикса Fwd и без разделителя в теле остаётся ответом
    # пользователя, у которого эвристика цитат съела весь текст
    raw = (
        "From: a@b.ru\r\nSubject: Кадры\r\nMessage-ID: <q1@b>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "От: boss@company.ru\r\nКому: dept@company.ru\r\n\r\n"
        "Готовим сокращение отдела продаж\r\n"
    ).encode("utf-8")

    parsed = parse_email(email.message_from_bytes(raw))

    assert parsed.is_forward is False
    assert "Готовим сокращение" in parsed.body
