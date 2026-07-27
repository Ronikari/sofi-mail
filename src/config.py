"""Единая точка чтения конфигурации из переменных окружения.

Все модули берут настройки отсюда — env нигде больше не читается, поэтому
дефолты не расходятся между демоном, CLI-командами и тестами.
"""

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()

# корень проекта: относительные пути из .env считаются от него, а не от cwd,
# иначе `python -m src.cli` из другой директории создаст вторую пустую БД
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _path(name: str, default: str) -> Path:
    raw = Path(os.getenv(name, default))
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


# --- Почтовый ящик модели ---
MAIL_ADDRESS = os.getenv("MAIL_ADDRESS", "").strip()
# Gmail показывает app password группами по 4 символа; пробелы внутри пароля
# ломают LOGIN, поэтому убираем их молча, а не заставляем чинить .env вручную
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "").replace(" ", "")
MAIL_DISPLAY_NAME = os.getenv("MAIL_DISPLAY_NAME", "Local LLM")
# У публичных провайдеров логин совпадает с адресом, в Exchange — часто нет:
# логином бывает CORP\svc-llm или UPN, а письма уходят от имени общего ящика
# (для этого учётке нужно право Send As).
MAIL_LOGIN = os.getenv("MAIL_LOGIN", "").strip() or MAIL_ADDRESS

# Как ходим за почтой: imap (IMAP + SMTP, публичные провайдеры и Exchange
# с включённой службой IMAP4) или ews (Exchange Web Services — когда IMAP/SMTP
# закрыты или оставлена только NTLM/Kerberos-аутентификация)
MAIL_TRANSPORT = os.getenv("MAIL_TRANSPORT", "imap").strip().lower()

# --- TLS ---
# stdlib при context=None берёт ssl._create_stdlib_context(): CERT_NONE и
# check_hostname=False, то есть сертификат не проверяется вообще. Для внешних
# провайдеров это надо включать, для Exchange с внутренним УЦ — указать его
# корневой сертификат в MAIL_CA_FILE, иначе проверка не пройдёт.
MAIL_TLS_VERIFY = _bool("MAIL_TLS_VERIFY", True)
MAIL_CA_FILE = os.getenv("MAIL_CA_FILE", "").strip()

# --- IMAP (приём) ---
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
# false: подключаться на 993 сразу по SSL. true: 143 + STARTTLS — внутри
# корпоративного периметра часто открыт только этот вариант
IMAP_STARTTLS = _bool("IMAP_STARTTLS", False)

# --- SMTP (отправка) ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
# starttls (587) | ssl (465) | none — без шифрования вовсе. Последнее нужно
# коннектору внутреннего релея Exchange на 25-м порту и локальному приёмнику
# при отладке; старый булев SMTP_STARTTLS продолжает работать как раньше
SMTP_TLS = os.getenv("SMTP_TLS", "").strip().lower() or (
    "starttls" if _bool("SMTP_STARTTLS", True) else "ssl"
)
# Коннектор внутреннего релея Exchange авторизацию обычно не объявляет вовсе,
# и login() падает с SMTPNotSupportedError, хотя письмо ушло бы и без неё
SMTP_AUTH = _bool("SMTP_AUTH", True)

# --- EWS (Exchange Web Services) ---
# Пусто — включается autodiscover по домену адреса; в закрытых сетях
# autodiscover часто недоступен, тогда имя сервера задаётся явно
EWS_SERVER = os.getenv("EWS_SERVER", "").strip()
# Полный URL точки входа, если он нестандартный (иначе строится как
# https://<EWS_SERVER>/EWS/Exchange.asmx)
EWS_ENDPOINT = os.getenv("EWS_ENDPOINT", "").strip()
# basic | ntlm | gssapi | sspi | digest | oauth2; пусто — определить самому.
# gssapi/sspi работают по билету Kerberos, пароль при них не нужен
EWS_AUTH = os.getenv("EWS_AUTH", "").strip().lower()
# delegate — учётке выданы права на ящик; impersonation — служебная учётка
# работает «от имени» ящика (ApplicationImpersonation)
EWS_ACCESS_TYPE = os.getenv("EWS_ACCESS_TYPE", "delegate").strip().lower()
# Папка, из которой читаем. inbox — «Входящие»; можно указать вложенную
# путём вида "Входящие/LLM", если письма раскладывает серверное правило
EWS_FOLDER = os.getenv("EWS_FOLDER", "inbox").strip()

# --- OAuth2 для EWS (EWS_AUTH=oauth2) ---
# Данные регистрации приложения в Entra ID (Azure AD). Нужны там, где Basic
# и NTLM отключены: Exchange Online принимает только OAuth2, локальный
# Exchange — в гибридном режиме с современной аутентификацией.
EWS_CLIENT_ID = os.getenv("EWS_CLIENT_ID", "").strip()
EWS_CLIENT_SECRET = os.getenv("EWS_CLIENT_SECRET", "").strip()
# Пусто — Microsoft выберет тенант сам (common); для служебной учётки лучше указать
EWS_TENANT_ID = os.getenv("EWS_TENANT_ID", "").strip()

# --- Ollama (LLM) ---
# Нативный API Ollama (порт 11434, без суффикса /v1): через /v1 нельзя передать
# num_ctx, а дефолтных 4096 не хватает на историю переписки
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").removesuffix("/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:4b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", 16384))
# у qwen3 рассуждения не отключаются, и num_predict должен покрывать
# thinking (~2-3.5K токенов) И сам ответ — отсюда бюджет 6144
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", 6144))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", 0.6))
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", 300))
SYSTEM_PROMPT_FILE = _path("SYSTEM_PROMPT_FILE", "prompts/system.txt")

# --- Поведение демона ---
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", 20))
MARK_SEEN = _bool("MARK_SEEN", True)
DRY_RUN = _bool("DRY_RUN", False)
# Сколько писем обрабатывается одновременно. 1 — строго по одному (личный ящик).
# Для сервиса на компанию поднимать вместе с OLLAMA_NUM_PARALLEL на стороне
# Ollama: без этого запросы всё равно встанут в очередь внутри самой Ollama.
WORKERS = max(1, int(os.getenv("WORKERS", 1)))

# --- Доступ и лимиты ---
ALLOWED_SENDERS: List[str] = [
    addr.strip().lower() for addr in os.getenv("ALLOWED_SENDERS", "").split(",") if addr.strip()
]
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", 20000))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", 40))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", 20))

# --- Сессии и хранилище ---
DB_PATH = _path("DB_PATH", "data/sessions.db")
THREAD_BY_SUBJECT = _bool("THREAD_BY_SUBJECT", True)


def is_sender_allowed(address: str) -> bool:
    """Проверка адреса по whitelist.

    Запись вида `@company.ru` разрешает весь домен — иначе список пришлось бы
    вести по каждому коллеге вручную.
    """
    address = address.lower().strip()
    if not address:
        return False
    domain = "@" + address.split("@")[-1]
    return any(allowed in (address, domain) for allowed in ALLOWED_SENDERS)


def _oauth2_problems() -> List[str]:
    """Проверки, осмысленные только при EWS_AUTH=oauth2.

    Entra ID на неполный набор параметров отвечает кодом вида AADSTS900023 без
    внятного текста, поэтому дешевле поймать это на старте.
    """
    if MAIL_TRANSPORT != "ews" or EWS_AUTH != "oauth2":
        return []

    problems = [
        f"{name} не задан: при EWS_AUTH=oauth2 нужна регистрация приложения в Entra ID"
        for name, value in (
            ("EWS_CLIENT_ID", EWS_CLIENT_ID),
            ("EWS_CLIENT_SECRET", EWS_CLIENT_SECRET),
        )
        if not value
    ]
    # Без пароля токен выдаётся приложению, а не пользователю, и контекста ящика
    # в нём нет. Exchange примет такой запрос только с заголовком impersonation,
    # иначе ответит ErrorAccessDenied уже на первом чтении папки
    if not MAIL_PASSWORD and EWS_ACCESS_TYPE != "impersonation":
        problems.append(
            "EWS_AUTH=oauth2 без MAIL_PASSWORD — это доступ от имени приложения, "
            "он работает только с EWS_ACCESS_TYPE=impersonation (учётке нужно "
            "право ApplicationImpersonation). Либо задайте MAIL_PASSWORD, чтобы "
            "приложение действовало от имени пользователя"
        )
    return problems


def validate() -> None:
    """Проверка настроек, без которых демон не имеет смысла.

    Вызывается в начале команд, работающих с почтой. Ошибка на старте понятнее,
    чем падение в середине обработки письма: пустой ALLOWED_SENDERS, например,
    привёл бы к тому, что демон молча игнорирует вообще все входящие.
    """
    problems = []
    if MAIL_TRANSPORT not in ("imap", "ews"):
        problems.append(f"MAIL_TRANSPORT={MAIL_TRANSPORT!r}: допустимы только 'imap' и 'ews'")
    if not MAIL_ADDRESS:
        problems.append("MAIL_ADDRESS не задан")
    # Пароль не нужен там, где аутентификация идёт не по нему: Kerberos берёт
    # билет из кеша, а OAuth2 без пароля работает от имени приложения
    password_optional = MAIL_TRANSPORT == "ews" and EWS_AUTH in ("gssapi", "sspi", "oauth2")
    if not MAIL_PASSWORD and not password_optional:
        problems.append("MAIL_PASSWORD не задан (для Gmail нужен App Password, не пароль аккаунта)")
    problems.extend(_oauth2_problems())
    if SMTP_TLS not in ("starttls", "ssl", "none"):
        problems.append(f"SMTP_TLS={SMTP_TLS!r}: допустимы 'starttls', 'ssl' и 'none'")
    if MAIL_CA_FILE and not Path(MAIL_CA_FILE).exists():
        problems.append(f"MAIL_CA_FILE={MAIL_CA_FILE}: файл не найден")
    if not ALLOWED_SENDERS:
        problems.append(
            "ALLOWED_SENDERS пуст — демон отвечал бы никому. "
            "Укажите хотя бы свой адрес."
        )
    if problems:
        raise ValueError("Ошибки конфигурации (.env):\n  - " + "\n  - ".join(problems))
