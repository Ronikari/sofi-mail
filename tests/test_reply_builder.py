# тесты сборки ответного письма.
# порядок: build_footer и build_reply вызываются напрямую -> утверждение
# сверяет состав подписи и заголовки треда.
# вход: значения config подменяются через monkeypatch.
# выход: результат pytest.
# проверяется reply_builder.py; константа REPLY_MARKER приходит
# из email_parser.py.
# сеть и база здесь не используются.
# запуск: pytest tests/test_reply_builder.py

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


def test_reply_body_ends_with_the_footer(monkeypatch):
    """Тело письма заканчивается подписью с маркером."""
    monkeypatch.setattr(reply_builder, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(reply_builder, "MAIL_DISPLAY_NAME", "Sofi")

    message = reply_builder.build_reply(
        to_address="ivan@company.ru",
        subject="Вопрос",
        body="Ответ модели",
        session_title="Вопрос",
    )

    body = message.get_payload(decode=True).decode("utf-8")
    assert body.startswith("Ответ модели")
    assert body.rstrip().endswith(reply_builder.build_footer("Вопрос"))
