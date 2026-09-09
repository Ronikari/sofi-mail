# тесты сборки ответного письма.
# порядок: build_footer и build_reply вызываются напрямую -> утверждение
# сверяет состав подписи и заголовки треда.
# вход: значения config подменяются через monkeypatch.
# выход: результат pytest.
# проверяется reply_builder.py; константа REPLY_MARKER приходит
# из email_parser.py.
# сеть и база здесь не используются.
# запуск: pytest tests/test_reply_builder.py

import base64

import pytest

from src import reply_builder
from src.email_parser import REPLY_MARKER


def test_footer_carries_only_marker_and_session(monkeypatch):
    """Подпись состоит из маркера и названия сессии."""
    # отображаемое имя пользователь видит в поле «От», а идентификатор модели
    # относится к внутренней настройке сервиса: в подписи они дублировали бы
    # заголовок письма и раскрывали бы устройство сервиса
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    footer = reply_builder.build_footer("Вопрос про Python")

    assert footer == f"-- \n{REPLY_MARKER} · сессия «Вопрос про Python»"
    assert "Sofi ·" not in footer, "отображаемое имя дублирует поле «От»"


def test_footer_keeps_rfc_delimiter_and_marker():
    """Подпись сохраняет разделитель RFC 3676 и технический маркер."""
    # по этим двум признакам email_parser.strip_quoted отрезает цитату
    # в ответе пользователя
    footer = reply_builder.build_footer("Тема")

    assert footer.startswith("-- \n")
    assert REPLY_MARKER in footer


def test_reply_body_starts_with_the_marker_and_ends_with_the_footer(monkeypatch):
    """Тело письма открывается меткой [Sofi] и заканчивается подписью."""
    monkeypatch.setattr(reply_builder, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    message = reply_builder.build_reply(
        to_address="ivan@company.ru",
        subject="Вопрос",
        body="Ответ модели",
        session_title="Вопрос",
    )

    body = message.get_payload(decode=True).decode("utf-8")
    assert body.startswith(f"{REPLY_MARKER} Ответ модели")
    assert body.rstrip().endswith(reply_builder.build_footer("Вопрос"))


def test_marker_is_not_doubled():
    """Метка ставится один раз: pipeline помечает ответ до сборки письма."""
    once = reply_builder.mark_answer("Ответ модели")

    assert once == f"{REPLY_MARKER} Ответ модели"
    assert reply_builder.mark_answer(once) == once


def test_reply_continues_the_exchange_conversation(monkeypatch):
    """Ответ несёт заголовки разговора Exchange: Outlook кладёт его в тот же тред."""
    monkeypatch.setattr(reply_builder, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    parent = reply_builder.next_thread_index()

    message = reply_builder.build_reply(
        to_address="ivan@company.ru",
        subject="Re: Вопрос",
        body="Ответ модели",
        session_title="Вопрос",
        in_reply_to="<u1@mail>",
        thread_index=parent,
        incoming_topic="Вопрос",
    )

    child = base64.b64decode(message["Thread-Index"])

    # заголовочный блок разговора совпадает с блоком входящего письма: по нему
    # Exchange относит ответ к разговору, а не открывает новый
    assert child[:22] == base64.b64decode(parent)[:22]
    assert len(child) == 27, "к разговору не добавился блок шага"
    assert message["Thread-Topic"] == "Вопрос"
    assert message["In-Reply-To"] == "<u1@mail>"


def test_reply_carries_a_quote_of_the_incoming_message(monkeypatch):
    """Ответ несёт видимую цитату входящего письма после подписи с маркером."""
    monkeypatch.setattr(reply_builder, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    message = reply_builder.build_reply(
        to_address="ivan@company.ru",
        subject="Вопрос",
        body="Ответ модели",
        session_title="Вопрос",
        sender_name="Иван Иванов",
        quoted_body="Текст вопроса пользователя",
        sent_date="Tue, 09 Sep 2026 10:00:00 +0300",
    )

    body = message.get_payload(decode=True).decode("utf-8")
    footer = reply_builder.build_footer("Вопрос")

    # цитата стоит строго после подписи: strip_own_replies режет тело
    # по REPLY_MARKER раньше, чем доходит до неё
    assert footer in body
    assert body.index(footer) < body.index("От: Иван Иванов <ivan@company.ru>")
    assert "Отправлено: 9 сентября 2026 г. 10:00" in body
    assert "Кому: Sofi <llm@company.ru>" in body
    assert "Тема: Вопрос" in body
    assert body.rstrip().endswith("Текст вопроса пользователя")


def test_reply_has_no_quote_block_without_quoted_body(monkeypatch):
    """Пустой quoted_body отключает цитату целиком, включая шапку."""
    monkeypatch.setattr(reply_builder, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    message = reply_builder.build_reply(
        to_address="ivan@company.ru",
        subject="Вопрос",
        body="Ответ модели",
        session_title="Вопрос",
    )

    body = message.get_payload(decode=True).decode("utf-8")
    assert "От:" not in body


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Tue, 09 Sep 2026 10:00:00 +0300", "9 сентября 2026 г. 10:00"),
        # однозначный день без ведущего нуля — так пишет и Outlook
        ("Thu, 1 Jan 2026 09:05:00 +0300", "1 января 2026 г. 09:05"),
        ("Wed, 31 Dec 2025 23:59:00 +0300", "31 декабря 2025 г. 23:59"),
    ],
)
def test_sent_date_is_formatted_like_outlook(raw, expected):
    """Дата цитаты пишется по-русски, а не заголовком RFC 5322."""
    assert reply_builder.format_sent_date(raw) == expected


def test_sent_date_is_normalised_to_one_timezone():
    """Один и тот же момент времени даёт одну строку при любом смещении."""
    # без приведения к поясу машины эти два заголовка показали бы разное
    # время на часах: 07:00 и 10:00. заголовок Date исходящего письма
    # ставится тем же поясом, и обе даты письма читаются в одной шкале
    utc = reply_builder.format_sent_date("Tue, 09 Sep 2026 07:00:00 +0000")
    msk = reply_builder.format_sent_date("Tue, 09 Sep 2026 10:00:00 +0300")

    assert utc == msk


@pytest.mark.parametrize("raw", ["не дата", ""])
def test_unreadable_date_is_kept_as_is(raw):
    """Неразобранный заголовок Date уходит в цитату исходной строкой."""
    # потерять дату хуже, чем показать её в чужом формате
    assert reply_builder.format_sent_date(raw) == raw


def test_thread_index_starts_a_conversation_without_a_parent():
    """Первое письмо без Thread-Index получает новый корень разговора."""
    root = base64.b64decode(reply_builder.next_thread_index(""))

    assert len(root) == 22
    assert root[0] == 1

    # испорченное значение заголовка тоже даёт корень, а не исключение
    assert len(base64.b64decode(reply_builder.next_thread_index("не base64!"))) == 22
