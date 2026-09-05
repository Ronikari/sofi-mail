"""Проверки конфигурации, которые должны срабатывать на старте, а не в бою.

Основной предмет здесь — OAuth2 для EWS: он распадается на два разных потока,
и неполный набор параметров Entra ID возвращает кодом вида AADSTS без пояснений.
"""

import ast
from pathlib import Path

import pytest

from src import config

SRC = Path(__file__).parent.parent / "src"


@pytest.fixture
def ews_oauth2(monkeypatch):
    """Минимально валидный конфиг EWS с OAuth2 от имени приложения.

    Задаются и переменные, к OAuth2 отношения не имеющие: config читает .env
    разработчика при импорте, и без этого тест падал бы от чужой настройки —
    например от MAIL_CA_FILE с несуществующим путём.
    """
    monkeypatch.setattr(config, "MAIL_CA_FILE", "")
    monkeypatch.setattr(config, "MAIL_TLS_VERIFY", True)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://sofi.company.ru/api")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "LLM_CA_FILE", "")
    monkeypatch.setattr(config, "LLM_ALLOW_INSECURE", False)
    monkeypatch.setattr(config, "MAIL_ADDRESS", "llm@company.ru")
    monkeypatch.setattr(config, "MAIL_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["ceo@company.ru"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", False)
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


def test_llm_address_is_required(ews_oauth2):
    """Модель отдаёт Open WebUI компании, поэтому дефолта у адреса нет.

    Без проверки пустой адрес выглядел бы как «сервер недоступен», и искать
    причину пришлось бы на чужой машине, а не в своём .env.
    """
    ews_oauth2.setattr(config, "LLM_BASE_URL", "")

    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        config.validate()


def test_vllm_style_address_is_rejected(ews_oauth2):
    """Хвост /v1 остался от прямого обращения к vLLM и молча ломает генерацию.

    В Open WebUI по этому пути внутренний REST (чаты, знания), а не completions:
    запрос упёрся бы в 404 в глубине клиента.
    """
    ews_oauth2.setattr(config, "LLM_BASE_URL", "https://sofi.company.ru/api/v1")

    with pytest.raises(ValueError, match="/v1"):
        config.validate()


def test_llm_api_key_is_required(ews_oauth2):
    """Open WebUI без ключа отвечает 401 — в отличие от vLLM, где он был необязателен."""
    ews_oauth2.setattr(config, "LLM_API_KEY", "")

    with pytest.raises(ValueError, match="LLM_API_KEY"):
        config.validate()


# --- защита переписки: транспорт и ширина доступа ---------------------------


def test_plaintext_llm_address_is_rejected(ews_oauth2):
    """По http к модели уходит открытым текстом всё письмо и вся история сессии."""
    ews_oauth2.setattr(config, "LLM_BASE_URL", "http://sofi.company.ru/api")

    with pytest.raises(ValueError, match="LLM_ALLOW_INSECURE"):
        config.validate()


def test_plaintext_llm_address_allowed_when_confirmed(ews_oauth2):
    """Изолированный сегмент — законный случай, но решение должно быть явным."""
    ews_oauth2.setattr(config, "LLM_BASE_URL", "http://sofi.company.ru/api")
    ews_oauth2.setattr(config, "LLM_ALLOW_INSECURE", True)

    config.validate()


def test_domain_wildcard_is_rejected_by_default(ews_oauth2):
    """Доменная запись пускает любого сотрудника, включая получателя пересылки."""
    ews_oauth2.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])

    with pytest.raises(ValueError, match="ALLOW_DOMAIN_WILDCARD"):
        config.validate()


def test_domain_wildcard_allowed_when_confirmed(ews_oauth2):
    ews_oauth2.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    ews_oauth2.setattr(config, "ALLOW_DOMAIN_WILDCARD", True)

    config.validate()


def test_disabled_mail_tls_verify_is_rejected(ews_oauth2):
    """MAIL_TLS_VERIFY=false — режим отладки, в боевом контуре это перехват почты."""
    ews_oauth2.setattr(config, "MAIL_TLS_VERIFY", False)

    with pytest.raises(ValueError, match="MAIL_TLS_VERIFY"):
        config.validate()


def test_domain_wildcard_does_not_match_without_opt_in(monkeypatch):
    """Проверка адреса дублирует запрет: конфиг мог собраться в обход validate."""
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", False)
    assert not config.is_sender_allowed("intern@company.ru")

    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", True)
    assert config.is_sender_allowed("intern@company.ru")


def test_every_imported_setting_exists():
    """Все имена, которые модули берут из config, должны в нём быть.

    Тяжёлые импорты в проекте лежат внутри функций (`--help` не должен ждать
    загрузки langchain), поэтому опечатка или ссылка на удалённую переменную
    не видна ни при импорте модуля, ни в тестах — она выстреливает только когда
    дойдёт очередь до этой команды. На боевом Exchange это худшее место для
    сюрприза, поэтому имена сверяются статически.
    """
    missing = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "src.config":
                continue
            missing += [
                f"{path.name}: {alias.name}"
                for alias in node.names
                if not hasattr(config, alias.name)
            ]

    assert not missing, "в src/config.py нет: " + ", ".join(missing)


def test_langchain_tracing_is_forced_off():
    """Одна строка LANGCHAIN_TRACING_V2=true отправила бы промпты в облако."""
    import os

    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert "LANGSMITH_API_KEY" not in os.environ


