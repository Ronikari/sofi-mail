# конфигурация проекта из .env — единственное место чтения окружения (демон, cli, тесты совпадают).
# настройки модели (промпт, генерация, знания, фильтры) заданы на sofi-mail в Open WebUI, не здесь.

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()

# Гасим телеметрию LangSmith принудительно: иначе переписка компании уходит в облако.
# Присваивание, не setdefault — иначе можно включить через окружение.
for _telemetry_var in (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGSMITH_TRACING",
    "LANGCHAIN_WANDB_TRACING",
):
    os.environ[_telemetry_var] = "false"

# ключи и адрес сборщика удаляются целиком: при пустом значении клиент
# LangSmith пропускает отправку
for _telemetry_var in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_ENDPOINT"):
    os.environ.pop(_telemetry_var, None)

# корень проекта. относительные пути из .env отсчитываются от него: запуск
# `python -m src.cli` из другого каталога создал бы вторую пустую базу
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# True для написаний 1, true, yes, on в любом регистре, иначе False.
def _bool(name: str, default: bool) -> bool:
    """Читает переменную окружения как булев флаг."""
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# вход: имя переменной окружения и путь по умолчанию.
# выход: абсолютный путь; относительное значение достраивается от PROJECT_ROOT
def _path(name: str, default: str) -> Path:
    """Читает переменную окружения как путь файловой системы."""
    raw = Path(os.getenv(name, default))
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


# --- Почтовый ящик модели ---
MAIL_ADDRESS = os.getenv("MAIL_ADDRESS", "").strip()
# пароль служебной учётной записи домена. strip снимает крайние пробелы,
# внутренние сохраняются: они входят в пароль
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "").strip()
MAIL_DISPLAY_NAME = os.getenv("MAIL_DISPLAY_NAME", "Local LLM")
# логин в Exchange (CORP\svc-llm либо UPN) расходится с адресом ящика: письма
# уходят от имени общего ящика по праву Send As.
MAIL_LOGIN = os.getenv("MAIL_LOGIN", "").strip() or MAIL_ADDRESS

# --- TLS --- сертификат Exchange от внутреннего УЦ, корень — в MAIL_CA_FILE.
# MAIL_TLS_VERIFY=false отключает проверку для отладки.
MAIL_TLS_VERIFY = _bool("MAIL_TLS_VERIFY", True)
MAIL_CA_FILE = os.getenv("MAIL_CA_FILE", "").strip()

# --- EWS --- пустое значение включает autodiscover (поиск сервера по домену адреса);
# в закрытых сетях он недоступен, тогда имя сервера задаётся явно.
EWS_SERVER = os.getenv("EWS_SERVER", "").strip()
# полный адрес точки входа для нестандартного размещения; при пустом значении
# адрес строится как https://<EWS_SERVER>/EWS/Exchange.asmx
EWS_ENDPOINT = os.getenv("EWS_ENDPOINT", "").strip()
# basic | ntlm | gssapi | sspi | digest | oauth2; пустое значение оставляет выбор
# exchangelib. gssapi и sspi работают по билету Kerberos, без пароля.
EWS_AUTH = os.getenv("EWS_AUTH", "").strip().lower()
# delegate — права на ящик выданы учётной записи; impersonation — служебная
# учётная запись работает от имени ящика по праву ApplicationImpersonation
EWS_ACCESS_TYPE = os.getenv("EWS_ACCESS_TYPE", "delegate").strip().lower()
# предел ожидания ответа Exchange в секундах: без него запрос к недоступному
# серверу держит поток обработки письма до таймаута сокета ОС.
EWS_TIMEOUT_SEC = int(os.getenv("EWS_TIMEOUT_SEC", 120))
# папка приёма. inbox обозначает «Входящие»; вложенная папка задаётся путём
# вида "Входящие/LLM" для случая, когда письма раскладывает серверное правило
EWS_FOLDER = os.getenv("EWS_FOLDER", "inbox").strip()
# копия ответа в «Отправленных» служебного ящика — он общий на всех пользователей,
# поэтому владелец прав видит переписку каждого. Включается там, где нужна копия по политике хранения.
EWS_SAVE_SENT = _bool("EWS_SAVE_SENT", False)

# --- OAuth2 (EWS_AUTH=oauth2) --- регистрация приложения в Entra ID.
# Нужна, где Basic/NTLM отключены — Exchange Online принимает только OAuth2.
EWS_CLIENT_ID = os.getenv("EWS_CLIENT_ID", "").strip()
EWS_CLIENT_SECRET = os.getenv("EWS_CLIENT_SECRET", "").strip()
# пустое значение направляет запрос в тенант common, где Microsoft выбирает
# каталог сам; для служебной учётной записи идентификатор указывается явно
EWS_TENANT_ID = os.getenv("EWS_TENANT_ID", "").strip()

# --- LLM: шлюз Open WebUI --- модель sofi-mail заведена там под сервисной учёткой;
# проекту принадлежат только адрес, ключ и id модели.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
# идентификатор модели рабочего пространства (Workspace -> Models -> Model ID).
# имя базовой модели инференса направило бы запрос мимо sofi-mail — без промпта, знаний и фильтров.
LLM_MODEL = os.getenv("LLM_MODEL", "sofi-mail").strip()
# предел ожидания ответа модели в секундах
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", 300))
# ключ API сервисной учётки (Settings -> Account -> API keys): без него 401,
# и он же определяет, какие модели и базы знаний видит демон.
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
# адрес веб-интерфейса Open WebUI: уходит в письмо, когда документ нельзя обработать почтой.
# по умолчанию выводится из LLM_BASE_URL — заданные врозь адреса расходятся при переезде сервера.
LLM_WEB_URL = os.getenv("LLM_WEB_URL", "").strip().rstrip("/") or LLM_BASE_URL.removesuffix("/api")
# идентификаторы кастомных Tools (Workspace -> Tools), доступных модели запроса.
# пусто по умолчанию — без явного перечисления демон не полагается ни на один. встроенных тулов Open WebUI это не касается.
LLM_TOOL_IDS = [t.strip() for t in os.getenv("LLM_TOOL_IDS", "").split(",") if t.strip()]
# false оставляет чат в Open WebUI после ответа — на время наблюдения за Path A через UI.
LLM_DELETE_CHAT = _bool("LLM_DELETE_CHAT", True)

# --- TLS до модели --- по http текст письма и история идут открытым текстом в сети.
# Боевой адрес — https; LLM_CA_FILE задаёт корень внутреннего УЦ.
LLM_CA_FILE = os.getenv("LLM_CA_FILE", "").strip()
# разрешение на http: изолированный сегмент, отладка, туннель снаружи.
# при значении false демон с http-адресом останавливается на validate
LLM_ALLOW_INSECURE = _bool("LLM_ALLOW_INSECURE", False)

# --- Поведение демона ---
# пауза между опросами ящика в секундах
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", 20))
MARK_SEEN = _bool("MARK_SEEN", True)
DRY_RUN = _bool("DRY_RUN", False)
# число писем, обрабатываемых одновременно; верхнюю границу задаёт --max-num-seqs
# сервера vLLM, дальше рост значения не ускоряет обработку.
WORKERS = max(1, int(os.getenv("WORKERS", 1)))

# --- Доступ и лимиты ---
# whitelist адресов, разделённых запятыми; регистр приводится к нижнему
ALLOWED_SENDERS: List[str] = [
    addr.strip().lower() for addr in os.getenv("ALLOWED_SENDERS", "").split(",") if addr.strip()
]
# разрешение записей вида `@company.ru`, открывающих домен целиком — пускает любого
# сотрудника, включая получателя пересланного чужого треда, поэтому включается явно.
ALLOW_DOMAIN_WILDCARD = _bool("ALLOW_DOMAIN_WILDCARD", False)
# предел длины одного письма в символах; 0 снимает обрезку — иначе часть условий
# вопроса отрезалась бы без ведома модели.
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", 0))
# сколько реплик сессии читается из таблицы messages; 0 читает все — решение
# об объёме истории принимает свёртка сессии, а не эта выборка.
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", 0))
# порог суммарной длины запроса в символах (история + письмо); окно модели в токенах
# клиенту недоступно, ориентир — окно * 3 символа на токен, с запасом на ответ и знания.
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", 60000))
# предел блока описаний документов перед текстом письма, в символах — без него блок
# с письмом переваливает за MAX_CONTEXT_CHARS, и история отбрасывается целиком. 0 снимает предел.
MAX_ATTACHMENT_CONTEXT_CHARS = int(os.getenv("MAX_ATTACHMENT_CONTEXT_CHARS", 20000))
# сколько писем с одного адреса обрабатывается за час
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", 20))

# --- Суммаризация --- без свёртки длинная сессия упирается в окно модели, и
# сервер отвергает запрос целиком; выключение оставляет реплики расти как есть.
SUMMARY_ENABLED = _bool("SUMMARY_ENABLED", True)
# предел объёма сессии в символах: по достижении реплики, предшествующие текущему
# письму, заменяются одной сводкой. держится ниже MAX_CONTEXT_CHARS — разница отведена под письмо и вложения.
SESSION_MAX_CHARS = int(os.getenv("SESSION_MAX_CHARS", 40000))
# доля объёма сводки от свёрнутой переписки (0.2 — впятеро короче), модель держит её
# приблизительно. завышение приближает следующую свёртку, занижение теряет детали.
SUMMARY_COMPRESSION_RATIO = float(os.getenv("SUMMARY_COMPRESSION_RATIO", 0.2))

# --- Вложения --- документ уходит файлом в Open WebUI как есть, разбор делает сервер.
# Те же данные оказываются во втором хранилище — выключатель общий и явный.
ATTACHMENTS_ENABLED = _bool("ATTACHMENTS_ENABLED", True)
# предел размера одного файла в мегабайтах — ограничивает и трафик, и время разбора
# на стороне Open WebUI.
ATTACHMENT_MAX_MB = int(os.getenv("ATTACHMENT_MAX_MB", 20))
# сколько файлов берётся из одного письма; остальные пропускаются с примечанием
ATTACHMENT_MAX_COUNT = int(os.getenv("ATTACHMENT_MAX_COUNT", 5))
# сколько файлов сессии уходит в запрос с текущим письмом — без предела к двадцатому
# письму треда в запрос попадала бы вся папка собеседника.
ATTACHMENT_MAX_SESSION_FILES = int(os.getenv("ATTACHMENT_MAX_SESSION_FILES", 10))
# предел ожидания, пока Open WebUI разберёт и проиндексирует файл, в секундах
ATTACHMENT_PROCESS_TIMEOUT_SEC = int(os.getenv("ATTACHMENT_PROCESS_TIMEOUT_SEC", 120))
# срок жизни файла в Open WebUI в сутках, 0 отключает уборку. по умолчанию совпадает
# с RETENTION_DAYS — более короткий срок молча роняет документ из ещё живой сессии.
ATTACHMENT_RETENTION_DAYS = int(os.getenv("ATTACHMENT_RETENTION_DAYS", 90))
# разрешение фактически удалять файлы в Open WebUI по политике хранения (истёкший
# срок, purge, forget). Уборка брака без строки в session_files идёт мимо этого флага, force=True.
ATTACHMENT_DELETE_ENABLED = _bool("ATTACHMENT_DELETE_ENABLED", True)

# --- Сессии и хранилище ---
DB_PATH = _path("DB_PATH", "data/sessions.db")
THREAD_BY_SUBJECT = _bool("THREAD_BY_SUBJECT", True)
# хранение тела письма до отсечения цитаты: внутри цитаты едет вся прежняя переписка
# треда, включая реплики третьих лиц, не писавших сервису — поэтому по умолчанию выключено.
STORE_RAW_BODY = _bool("STORE_RAW_BODY", False)
# срок хранения переписки в сутках; 0 — бессрочно, ущерб от компрометации базы
# растёт без предела.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", 90))

# --- Журналирование --- без маскирования адрес и тема пишутся в лог (journald) как есть.
# По умолчанию false (маскируются); true — временно, на разбор проблемы.
LOG_PII = _bool("LOG_PII", False)


# True для адреса из ALLOWED_SENDERS и для домена при ALLOW_DOMAIN_WILDCARD=true.
def is_sender_allowed(address: str) -> bool:
    """Проверяет адрес отправителя по списку ALLOWED_SENDERS."""
    address = address.lower().strip()

    # значение без @ адресом не является: сюда попадает пустой заголовок From
    if not address or "@" not in address:
        return False

    # точное совпадение с записью списка
    if address in ALLOWED_SENDERS:
        return True

    # доменные записи отключены — прочие адреса отклоняются
    if not ALLOW_DOMAIN_WILDCARD:
        return False

    # сверка с записью формы @company.ru — на случай конфигурации, собранной без validate.
    domain = "@" + address.split("@")[-1]
    return domain in ALLOWED_SENDERS


# Entra ID отвечает на неполный набор параметров кодом вида AADSTS900023 без
# описания причины, поэтому набор проверяется на старте.
def _oauth2_problems() -> List[str]:
    """Собирает проблемы настройки, относящиеся к режиму EWS_AUTH=oauth2."""
    # прочие режимы аутентификации эти переменные не используют
    if EWS_AUTH != "oauth2":
        return []

    # идентификатор и секрет приложения требуются оба
    problems = [
        f"{name} не задан: при EWS_AUTH=oauth2 нужна регистрация приложения в Entra ID"
        for name, value in (
            ("EWS_CLIENT_ID", EWS_CLIENT_ID),
            ("EWS_CLIENT_SECRET", EWS_CLIENT_SECRET),
        )
        if not value
    ]

    # пустой пароль переводит режим в выдачу токена приложению — в нём нет контекста ящика,
    # и delegate вместо impersonation даст ErrorAccessDenied на первом чтении папки.
    if not MAIL_PASSWORD and EWS_ACCESS_TYPE != "impersonation":
        problems.append(
            "EWS_AUTH=oauth2 без MAIL_PASSWORD — это доступ от имени приложения, "
            "он работает только с EWS_ACCESS_TYPE=impersonation (учётке нужно "
            "право ApplicationImpersonation). Либо задайте MAIL_PASSWORD, чтобы "
            "приложение действовало от имени пользователя"
        )
    return problems


# выход: список текстов проблем адреса и ключа шлюза к модели
def _llm_problems() -> List[str]:
    """Собирает проблемы настройки шлюза Open WebUI."""
    problems = []

    # пустой адрес: значение по умолчанию у LLM_BASE_URL отсутствует
    if not LLM_BASE_URL:
        problems.append(
            "LLM_BASE_URL не задан: модель отдаёт Open WebUI компании, адрес "
            "нужно указать явно (https://llm.example.com/api)"
        )

    # суффикс /v1 остался от прямых обращений к vLLM — по этому пути Open WebUI держит
    # внутренний rest, генерации там нет, запрос получает 404 либо чужой обработчик.
    if LLM_BASE_URL.endswith("/v1"):
        problems.append(
            f"LLM_BASE_URL={LLM_BASE_URL} заканчивается на /v1 — это адрес vLLM. "
            "Open WebUI принимает генерацию по /api (POST /api/chat/completions), "
            "а /api/v1 — его внутренний REST. Уберите суффикс: "
            "https://llm.example.com/api"
        )

    # пустой ключ: Open WebUI отвечает 401 на запрос без заголовка Authorization
    if not LLM_API_KEY:
        problems.append(
            "LLM_API_KEY не задан: ключ API сервисной учётной записи Open WebUI "
            "(Settings -> Account -> API keys). Без него сервер отвечает 401 — "
            "в отличие от vLLM, где ключ был необязателен"
        )
    return problems


# выход: список текстов проблем, относящихся к передаче данных по сети
def _transport_problems() -> List[str]:
    """Собирает проблемы настройки tls для модели и для почты."""
    problems = []

    # http-адрес модели без явного разрешения: текст письма и история сессии
    # пойдут по сети открытым текстом
    if LLM_BASE_URL and not LLM_BASE_URL.startswith("https://") and not LLM_ALLOW_INSECURE:
        problems.append(
            f"LLM_BASE_URL={LLM_BASE_URL} — открытый http. В запросе к модели уходит "
            "полный текст письма и история переписки, по http это открытый текст "
            "в корпоративной сети. Укажите https-адрес (корневой сертификат внутреннего "
            "УЦ — в LLM_CA_FILE) либо, если сегмент изолирован, разрешите это явно: "
            "LLM_ALLOW_INSECURE=true"
        )

    # заданный путь к сертификату проверяется на существование: отсутствующий
    # файл даёт отказ проверки tls на первом запросе
    if LLM_CA_FILE and not Path(LLM_CA_FILE).exists():
        problems.append(f"LLM_CA_FILE={LLM_CA_FILE}: файл не найден")

    # отключённая проверка сертификата Exchange допускает перехват почты
    if not MAIL_TLS_VERIFY:
        problems.append(
            "MAIL_TLS_VERIFY=false отключает проверку сертификата Exchange — "
            "почта уязвима к перехвату. Это режим отладки, не для боевого контура: "
            "укажите корневой сертификат внутреннего УЦ в MAIL_CA_FILE"
        )
    return problems


# проверка на старте: коэффициент вне диапазона или завышенный предел сессии
# проявились бы только на длинной переписке, у первого набравшего сессию пользователя.
def _summary_problems() -> List[str]:
    """Собирает проблемы настройки свёртки сессии в сводку."""
    if not SUMMARY_ENABLED:
        return []

    problems = []

    # доля вне интервала (0; 1) лишает свёртку смысла: 0 требует пустую сводку,
    # значение от 1 разрешает сводку длиннее исходной переписки
    if not 0 < SUMMARY_COMPRESSION_RATIO < 1:
        problems.append(
            f"SUMMARY_COMPRESSION_RATIO={SUMMARY_COMPRESSION_RATIO}: доля объёма сводки "
            "от свёрнутой переписки, значение задаётся в интервале от 0 до 1 "
            "(0.2 — сводка впятеро короче)"
        )

    if SESSION_MAX_CHARS <= 0:
        problems.append(
            f"SESSION_MAX_CHARS={SESSION_MAX_CHARS}: предел объёма сессии в символах, "
            "нулевое и отрицательное значение свернуло бы сессию на первом же письме. "
            "Свёртка выключается флагом SUMMARY_ENABLED=false"
        )

    # если предел сессии вместе с максимальным блоком вложений уже не меньше окна модели,
    # свёртка структурно не успевает сработать раньше отказа сервера — уже случавшийся регресс.
    elif (
        MAX_CONTEXT_CHARS
        and SESSION_MAX_CHARS + MAX_ATTACHMENT_CONTEXT_CHARS > MAX_CONTEXT_CHARS
    ):
        problems.append(
            f"SESSION_MAX_CHARS={SESSION_MAX_CHARS} + MAX_ATTACHMENT_CONTEXT_CHARS="
            f"{MAX_ATTACHMENT_CONTEXT_CHARS} больше MAX_CONTEXT_CHARS={MAX_CONTEXT_CHARS}: "
            "письмо с вложениями у обоих порогов сразу может дойти до окна модели раньше, "
            "чем сработает свёртка сессии. Уменьшите один из порогов так, чтобы сумма "
            "оставляла запас под текст самого письма"
        )

    return problems


# выход: список из одного текста при доменной записи без разрешения,
# пустой список в остальных случаях
def _access_problems() -> List[str]:
    """Собирает проблемы ширины доступа к сервису."""
    # записи списка, начинающиеся с @, открывают домен целиком
    wildcards = [addr for addr in ALLOWED_SENDERS if addr.startswith("@")]

    if wildcards and not ALLOW_DOMAIN_WILDCARD:
        return [
            f"ALLOWED_SENDERS содержит запись на весь домен ({', '.join(wildcards)}). "
            "Домен целиком пускает любого сотрудника — в том числе того, кому переслали "
            "чужой тред. Перечислите конкретные адреса или подтвердите решение явно: "
            "ALLOW_DOMAIN_WILDCARD=true"
        ]
    return []


# поднимает ValueError со списком всех найденных проблем: остановка на старте
# наступает раньше обработки первого письма.
def validate() -> None:
    """Проверяет набор настроек, без которых демон работать не может."""
    # проблемы накапливаются списком: одна ошибка за запуск заставила бы править
    # конфигурацию по кругу
    problems = []

    if not MAIL_ADDRESS:
        problems.append("MAIL_ADDRESS не задан")

    # три режима аутентификации работают без пароля: Kerberos и SSPI берут билет
    # из кеша, OAuth2 без пароля выдаёт токен приложению
    password_optional = EWS_AUTH in ("gssapi", "sspi", "oauth2")
    if not MAIL_PASSWORD and not password_optional:
        problems.append(
            "MAIL_PASSWORD не задан (пароль служебной учётной записи домена). "
            "Без пароля работают только EWS_AUTH=gssapi, sspi и oauth2"
        )

    problems.extend(_oauth2_problems())
    problems.extend(_llm_problems())

    # путь к сертификату Exchange проверяется на существование
    if MAIL_CA_FILE and not Path(MAIL_CA_FILE).exists():
        problems.append(f"MAIL_CA_FILE={MAIL_CA_FILE}: файл не найден")

    problems.extend(_transport_problems())
    problems.extend(_access_problems())
    problems.extend(_summary_problems())

    # пустой whitelist: демон читает ящик и пропускает каждое входящее письмо
    if not ALLOWED_SENDERS:
        problems.append(
            "ALLOWED_SENDERS пуст — демон отвечал бы никому. "
            "Укажите хотя бы свой адрес."
        )

    # найденные проблемы печатает cli._require_config одним списком
    if problems:
        raise ValueError("Ошибки конфигурации (.env):\n  - " + "\n  - ".join(problems))
