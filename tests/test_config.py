# тесты проверок конфигурации, срабатывающих на старте команд.
# порядок: фикстура ews_oauth2 подменяет набор настроек на минимально рабочий ->
# тест меняет одно значение -> config.validate поднимает ValueError либо проходит.
# вход: monkeypatch и исходники из src для статической сверки имён.
# выход: результат pytest.
# проверяется config.py; сеть и база здесь не используются.
# запуск: pytest tests/test_config.py
#
# основной предмет — режим OAuth2 для EWS: он распадается на два потока, и
# на неполный набор параметров Entra ID отвечает кодом вида AADSTS без описания

import ast
from pathlib import Path

import pytest

from src import config

SRC = Path(__file__).parent.parent / "src"


# побочный эффект: подмена пятнадцати значений модуля config.
# набор включает переменные, к OAuth2 отношения не имеющие: config читает .env
# разработчика на импорте, и чужая настройка (например MAIL_CA_FILE
# с несуществующим путём) роняла бы тест на посторонней проверке
@pytest.fixture
def ews_oauth2(monkeypatch):
    """Задаёт минимально рабочий конфиг EWS с OAuth2 от имени приложения."""
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

    # объект monkeypatch возвращается тестам: они меняют по одному значению
    return monkeypatch


def test_oauth2_application_access_needs_no_password(ews_oauth2):
    """Режим от имени приложения проходит проверку без пароля."""
    # токен в этом потоке выдаётся регистрации приложения, пароль пользователя
    # в обмене не участвует
    config.validate()


def test_oauth2_without_registration_is_rejected(ews_oauth2):
    """Пустые параметры регистрации приложения останавливают запуск."""
    ews_oauth2.setattr(config, "EWS_CLIENT_ID", "")
    ews_oauth2.setattr(config, "EWS_CLIENT_SECRET", "")

    with pytest.raises(ValueError) as error:
        config.validate()

    # в тексте названы оба недостающих имени: validate собирает проблемы списком
    assert "EWS_CLIENT_ID" in str(error.value)
    assert "EWS_CLIENT_SECRET" in str(error.value)


def test_application_access_without_impersonation_is_rejected(ews_oauth2):
    """Токен приложения с типом доступа delegate останавливает запуск."""
    # Exchange отвечает на такую пару ErrorAccessDenied при чтении папки,
    # и по этому ответу режим доступа как причина не определяется
    ews_oauth2.setattr(config, "EWS_ACCESS_TYPE", "delegate")

    with pytest.raises(ValueError, match="impersonation"):
        config.validate()


def test_oauth2_with_password_allows_delegate(ews_oauth2):
    """Пароль вместе с типом доступа delegate проходит проверку."""
    # с паролем приложение действует от имени пользователя, контекст ящика
    # в токене есть
    ews_oauth2.setattr(config, "EWS_ACCESS_TYPE", "delegate")
    ews_oauth2.setattr(config, "MAIL_PASSWORD", "secret")

    config.validate()


def test_llm_address_is_required(ews_oauth2):
    """Пустой адрес шлюза модели останавливает запуск."""
    # значения по умолчанию у адреса нет: пустая строка без проверки дала бы
    # ошибку «сервер недоступен», и причину искали бы на стороне сервера
    ews_oauth2.setattr(config, "LLM_BASE_URL", "")

    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        config.validate()


def test_vllm_style_address_is_rejected(ews_oauth2):
    """Адрес с хвостом /v1 останавливает запуск."""
    # по этому пути Open WebUI держит внутренний rest (чаты, знания), генерации
    # там нет: запрос получил бы 404 внутри клиента
    ews_oauth2.setattr(config, "LLM_BASE_URL", "https://sofi.company.ru/api/v1")

    with pytest.raises(ValueError, match="/v1"):
        config.validate()


def test_llm_api_key_is_required(ews_oauth2):
    """Пустой ключ API останавливает запуск."""
    # Open WebUI отвечает 401 на запрос без заголовка Authorization
    ews_oauth2.setattr(config, "LLM_API_KEY", "")

    with pytest.raises(ValueError, match="LLM_API_KEY"):
        config.validate()


# --- защита переписки: транспорт и ширина доступа ---------------------------


def test_plaintext_llm_address_is_rejected(ews_oauth2):
    """Адрес шлюза по http останавливает запуск."""
    # по http текст письма и история сессии идут по сети открытым текстом
    ews_oauth2.setattr(config, "LLM_BASE_URL", "http://sofi.company.ru/api")

    with pytest.raises(ValueError, match="LLM_ALLOW_INSECURE"):
        config.validate()


def test_plaintext_llm_address_allowed_when_confirmed(ews_oauth2):
    """Флаг LLM_ALLOW_INSECURE открывает адрес по http."""
    # изолированный сегмент сети образует законный случай, и решение принимается
    # явной записью в .env
    ews_oauth2.setattr(config, "LLM_BASE_URL", "http://sofi.company.ru/api")
    ews_oauth2.setattr(config, "LLM_ALLOW_INSECURE", True)

    config.validate()


def test_domain_wildcard_is_rejected_by_default(ews_oauth2):
    """Доменная запись в whitelist без разрешения останавливает запуск."""
    # запись открывает доступ любому сотруднику, включая получателя пересланного
    # чужого треда
    ews_oauth2.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])

    with pytest.raises(ValueError, match="ALLOW_DOMAIN_WILDCARD"):
        config.validate()


def test_domain_wildcard_allowed_when_confirmed(ews_oauth2):
    """Флаг ALLOW_DOMAIN_WILDCARD открывает доменную запись."""
    ews_oauth2.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    ews_oauth2.setattr(config, "ALLOW_DOMAIN_WILDCARD", True)

    config.validate()


def test_disabled_mail_tls_verify_is_rejected(ews_oauth2):
    """Отключённая проверка сертификата Exchange останавливает запуск."""
    # без проверки сертификата почта открыта перехвату; режим предназначен
    # для отладки
    ews_oauth2.setattr(config, "MAIL_TLS_VERIFY", False)

    with pytest.raises(ValueError, match="MAIL_TLS_VERIFY"):
        config.validate()


def test_domain_wildcard_does_not_match_without_opt_in(monkeypatch):
    """Проверка адреса учитывает доменную запись только при поднятом флаге."""
    # проверка дублирует запрет из validate: конфигурацию можно собрать
    # в обход validate
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["@company.ru"])
    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", False)
    assert not config.is_sender_allowed("intern@company.ru")

    monkeypatch.setattr(config, "ALLOW_DOMAIN_WILDCARD", True)
    assert config.is_sender_allowed("intern@company.ru")


def test_every_imported_setting_exists():
    """Каждое имя, импортируемое модулями из config, в нём объявлено."""
    # тяжёлые импорты проекта лежат внутри функций, поэтому опечатка в имени
    # и ссылка на удалённую переменную не проявляются ни при импорте модуля,
    # ни в остальных тестах: они срабатывают при вызове конкретной команды
    # на боевом Exchange. отсюда статическая сверка по дереву разбора
    missing = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # ast.walk обходит и вложенные узлы: импорты внутри функций попадают
        # в выборку наравне с импортами модуля
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
    """Импорт config гасит телеметрию LangChain в окружении процесса."""
    # значение LANGCHAIN_TRACING_V2=true отправляет каждый промпт вместе
    # с историей переписки в облако LangSmith
    import os

    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert "LANGCHAIN_API_KEY" not in os.environ
    assert "LANGSMITH_API_KEY" not in os.environ
