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


def test_thread_index_starts_a_conversation_without_a_parent():
    """Первое письмо без Thread-Index получает новый корень разговора."""
    root = base64.b64decode(reply_builder.next_thread_index(""))

    assert len(root) == 22
    assert root[0] == 1

    # испорченное значение заголовка тоже даёт корень, а не исключение
    assert len(base64.b64decode(reply_builder.next_thread_index("не base64!"))) == 22
