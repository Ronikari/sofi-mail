"""Проверки конфигурации, которые должны срабатывать на старте, а не в бою.

Основной предмет здесь — OAuth2 для EWS: он распадается на два разных потока,
и неполный набор параметров Entra ID возвращает кодом вида AADSTS без пояснений.
"""

import pytest

from src import config


@pytest.fixture
def ews_oauth2(monkeypatch):
    """Минимально валидный конфиг EWS с OAuth2 от имени приложения."""
    monkeypatch.setattr(config, "MAIL_TRANSPORT", "ews")
    monkeypatch.setattr(config, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(config, "MAIL_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    monkeypatch.setattr(config, "EWS_AUTH", "oauth2")
    monkeypatch.setattr(config, "EWS_ACCESS_TYPE", "impersonation")
    monkeypatch.setattr(config, "EWS_CLIENT_ID", "app-id")
    monkeypatch.setattr(config, "EWS_CLIENT_SECRET", "app-secret")
    monkeypatch.setattr(config, "EWS_TENANT_ID", "tenant")
    return monkeypatch


def test_oauth2_application_access_needs_no_password(ews_oauth2):
    """Пароля в этом режиме нет и не должно быть — токен выдаётся приложению."""
    config.validate()


def test_oauth2_without_registration_is_rejected(ews_oauth2):
    ews_oauth2.setattr(config, "EWS_CLIENT_ID", "")
    ews_oauth2.setattr(config, "EWS_CLIENT_SECRET", "")

    with pytest.raises(ValueError) as error:
        config.validate()

    assert "EWS_CLIENT_ID" in str(error.value)
    assert "EWS_CLIENT_SECRET" in str(error.value)


def test_application_access_without_impersonation_is_rejected(ews_oauth2):
    """delegate + токен приложения — самая частая ошибка настройки.

    Exchange отвечает на неё ErrorAccessDenied уже при чтении папки, и по этому
    ответу невозможно догадаться, что дело в режиме доступа.
    """
    ews_oauth2.setattr(config, "EWS_ACCESS_TYPE", "delegate")

    with pytest.raises(ValueError, match="impersonation"):
        config.validate()


def test_oauth2_with_password_allows_delegate(ews_oauth2):
    """С паролем приложение действует от имени пользователя — impersonation не нужен."""
    ews_oauth2.setattr(config, "EWS_ACCESS_TYPE", "delegate")
    ews_oauth2.setattr(config, "MAIL_PASSWORD", "secret")

    config.validate()


def test_password_is_still_required_for_imap(ews_oauth2):
    """EWS_AUTH из .env не должен отменять пароль на обычной почте."""
    ews_oauth2.setattr(config, "MAIL_TRANSPORT", "imap")

    with pytest.raises(ValueError, match="MAIL_PASSWORD"):
        config.validate()
