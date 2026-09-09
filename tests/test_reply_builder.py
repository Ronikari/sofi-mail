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
import email

import pytest

from src import reply_builder
from src.email_parser import REPLY_MARKER, extract_body


# вход: собранное письмо и подтип текстовой части (plain либо html).
# выход: текст этой части, декодированный из utf-8.
# письмо многочастное, поэтому get_payload на нём отдаёт список частей,
# а не текст
def part_text(message, subtype: str) -> str:
    """Достаёт текст части письма заданного подтипа."""
    for part in message.walk():
        if part.get_content_type() == f"text/{subtype}":
            return part.get_payload(decode=True).decode("utf-8")

    raise AssertionError(f"в письме нет части text/{subtype}")


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

    body = part_text(message, "plain")
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

    body = part_text(message, "plain")
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

    assert "От:" not in part_text(message, "plain")
    assert "<b>От:</b>" not in part_text(message, "html")


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


def test_reply_carries_both_text_and_html_parts(monkeypatch):
    """Письмо уходит двумя частями, и text/plain стоит первой."""
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

    assert message.get_content_type() == "multipart/alternative"

    # порядок частей задан RFC 2046: ведущей считается последняя, поэтому
    # html идёт после plain. наш email_parser.extract_body читает plain
    subtypes = [
        part.get_content_type()
        for part in message.walk()
        if part.get_content_maintype() != "multipart"
    ]
    assert subtypes == ["text/plain", "text/html"]


def test_html_part_repeats_the_text_part(monkeypatch):
    """html-часть несёт тот же ответ, подпись и цитату, что и текстовая."""
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

    markup = part_text(message, "html")

    assert f"{REPLY_MARKER} Ответ модели" in markup
    assert "сессия «Вопрос»" in markup
    # метки шапки полужирные, как в цитате Outlook
    assert "<b>От:</b> Иван Иванов &lt;ivan@company.ru&gt;" in markup
    assert "<b>Отправлено:</b> 9 сентября 2026 г. 10:00" in markup
    assert "<b>Кому:</b> Sofi &lt;llm@company.ru&gt;" in markup
    assert "<b>Тема:</b> Вопрос" in markup
    assert "Текст вопроса пользователя" in markup

    # цитата в html идёт после подписи тем же порядком, что и в тексте
    assert markup.index("сессия «Вопрос»") < markup.index("<b>От:</b>")


def test_html_part_escapes_text_of_the_model_and_the_user(monkeypatch):
    """Разметка в тексте письма экранируется и тегом не становится."""
    monkeypatch.setattr(reply_builder, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    message = reply_builder.build_reply(
        to_address="ivan@company.ru",
        subject="Вопрос",
        body="Ответ <script>alert(1)</script> модели",
        session_title="Вопрос",
        sender_name="Иван <b>Иванов</b>",
        quoted_body="Вопрос про <div> и & в тексте",
        sent_date="Tue, 09 Sep 2026 10:00:00 +0300",
    )

    markup = part_text(message, "html")

    assert "<script>" not in markup
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markup
    assert "Иван &lt;b&gt;Иванов&lt;/b&gt;" in markup
    assert "&lt;div&gt; и &amp; в тексте" in markup


def test_own_reply_is_read_back_from_the_text_part(monkeypatch):
    """Собственный ответ, вернувшийся во входящие, читается частью text/plain."""
    # html-часть разбор не меняет: extract_body предпочитает text/plain,
    # и граница цитаты по REPLY_MARKER остаётся на месте
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

    parsed = email.message_from_bytes(message.as_bytes())
    text = extract_body(parsed)

    assert text.startswith(f"{REPLY_MARKER} Ответ модели")
    assert "<html>" not in text
    assert "От: Иван Иванов <ivan@company.ru>" in text


def test_thread_index_starts_a_conversation_without_a_parent():
    """Первое письмо без Thread-Index получает новый корень разговора."""
    root = base64.b64decode(reply_builder.next_thread_index(""))

    assert len(root) == 22
    assert root[0] == 1

    # испорченное значение заголовка тоже даёт корень, а не исключение
    assert len(base64.b64decode(reply_builder.next_thread_index("не base64!"))) == 22
