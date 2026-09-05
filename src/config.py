"""Единая точка чтения конфигурации из переменных окружения.

Все модули берут настройки отсюда — env нигде больше не читается, поэтому
дефолты не расходятся между демоном, CLI-командами и тестами.

Почта — Microsoft Exchange через EWS, модель — sofi-mail в Open WebUI компании.
Другой почты и другого шлюза к модели в сборке нет.

Настройки самой модели здесь отсутствуют намеренно: системный промпт, параметры
генерации, базы знаний и фильтры живут на модели sofi-mail в Open WebUI, и
любое их дублирование в .env перекрывало бы настройку модели в запросе.
"""

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()

# --- Телеметрия LangChain: выключена принудительно ---------------------------
# load_dotenv() выше заливает весь .env в окружение процесса, а LangChain читает
# эти переменные напрямую. Одна строка LANGCHAIN_TRACING_V2=true — в .env, в
# юните systemd или в профиле пользователя — и каждый промпт вместе с историей
# переписки уходит в облако LangSmith. Для переписки должностных лиц это утечка
# за периметр компании, поэтому значения выставляются жёстко, а не setdefault:
# перекрыть их из окружения не должно быть возможно.
#
# Ставится ДО первого импорта langchain (config импортируется раньше llm_backend),
# иначе клиент успел бы прочитать окружение на импорте.
for _telemetry_var in (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGSMITH_TRACING",
    "LANGCHAIN_WANDB_TRACING",
):
    os.environ[_telemetry_var] = "false"
for _telemetry_var in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_ENDPOINT"):
    os.environ.pop(_telemetry_var, None)

# --- Телеметрия unstructured: выключена принудительно ------------------------
# Библиотека разбора вложений шлёт пинг на packages.unstructured.io при импорте
# и по факту каждого разбора: версия, тип документа, стратегия, наличие GPU.
# Содержимого документа там нет, но сам факт «на этом хосте разобрали 12 pdf
# и 3 docx» — это утечка сведений о работе с перепиской должностных лиц наружу,
# а в закрытом контуре ещё и лишний исходящий запрос с таймаутом на каждый файл.
# Выставляется до первого импорта unstructured (config импортируется раньше
# attachments), значения жёсткие: перекрыть их из окружения не должно быть можно.
for _telemetry_var in ("DO_NOT_TRACK", "SCARF_NO_ANALYTICS"):
    os.environ[_telemetry_var] = "1"

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
# Пароль служебной учётной записи домена. Обрезаются только крайние пробелы:
# внутренние значимы, в отличие от app password Gmail, где их печатали
# группами по 4 символа и вычищали молча (публичная почта отключена)
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "").strip()
MAIL_DISPLAY_NAME = os.getenv("MAIL_DISPLAY_NAME", "Local LLM")
# В Exchange логин обычно не равен адресу: это CORP\svc-llm или UPN, а письма
# уходят от имени общего ящика (для этого учётке нужно право Send As).
MAIL_LOGIN = os.getenv("MAIL_LOGIN", "").strip() or MAIL_ADDRESS

# --- TLS ---
# У корпоративного Exchange сертификат чаще всего выпущен внутренним УЦ,
# которого нет в системном хранилище. Тогда путь к его корневому сертификату
# (PEM) идёт в MAIL_CA_FILE, иначе проверка не пройдёт. MAIL_TLS_VERIFY=false —
# только на время отладки.
MAIL_TLS_VERIFY = _bool("MAIL_TLS_VERIFY", True)
MAIL_CA_FILE = os.getenv("MAIL_CA_FILE", "").strip()

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
# Класть ли копию ответа в «Отправленные» служебного ящика. По умолчанию нет:
# ящик один на всех пользователей сервиса, и его «Отправленные» превращаются
# в общий архив переписки — кто имеет права на ящик, читает ответы всем.
# История и без того лежит в БД, а у самого пользователя ответ остаётся
# в его почте. Включать, если правила хранения переписки требуют копии.
EWS_SAVE_SENT = _bool("EWS_SAVE_SENT", False)

# --- OAuth2 для EWS (EWS_AUTH=oauth2) ---
# Данные регистрации приложения в Entra ID (Azure AD). Нужны там, где Basic
# и NTLM отключены: Exchange Online принимает только OAuth2, локальный
# Exchange — в гибридном режиме с современной аутентификацией.
EWS_CLIENT_ID = os.getenv("EWS_CLIENT_ID", "").strip()
EWS_CLIENT_SECRET = os.getenv("EWS_CLIENT_SECRET", "").strip()
# Пусто — Microsoft выберет тенант сам (common); для служебной учётки лучше указать
EWS_TENANT_ID = os.getenv("EWS_TENANT_ID", "").strip()

# --- LLM: шлюз Open WebUI ---
# Демон ходит не прямо в vLLM, а в Open WebUI: в его рабочем пространстве под
# сервисной учётной записью заведена модель sofi-mail, и на ней висит всё, что
# определяет ответ, — системный промпт, параметры генерации, базы знаний и
# фильтры. Проекту остаются три вещи: адрес, ключ и идентификатор модели.
#
# Дефолта у адреса нет намеренно: пустое значение лучше молчаливого обращения
# к себе же, которое выглядело бы как «сервер недоступен» вместо «адрес не задан».
# Суффикс /api дописывается бэкендом, если его забыли, — см. src/llm_backend.py
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
# Идентификатор модели рабочего пространства (Workspace -> Models -> Model ID),
# а не путь репозитория на сервере инференса: имя базовой модели увело бы запрос
# мимо sofi-mail, то есть без промпта, знаний и фильтров — ответ пришёл бы,
# и разница заметна не сразу
LLM_MODEL = os.getenv("LLM_MODEL", "sofi-mail").strip()
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", 300))
# Ключ API сервисной учётной записи (Settings -> Account -> API keys). В отличие
# от vLLM, который без --api-key принимал любой токен, Open WebUI отвечает 401:
# ключ обязателен, и он же определяет, чью модель и чьи знания видит демон
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
# Адрес веб-интерфейса того же Open WebUI — единственная настройка LLM, которую
# видит не демон, а пользователь: он уходит в письмо тогда, когда документ
# почтой обработать нельзя и вопрос по нему нужно задать руками (см.
# src/attachments.py). Дефолт выводится из LLM_BASE_URL отбрасыванием
# служебного /api: это тот же сервер, и держать два адреса врозь — способ
# однажды прислать должностному лицу ссылку на сервер, которого уже нет.
LLM_WEB_URL = os.getenv("LLM_WEB_URL", "").strip().rstrip("/") or LLM_BASE_URL.removesuffix("/api")

# --- TLS до сервера модели ---
# В запросе к модели уходит полный текст письма и вся история сессии. По http
# это открытый текст в корпоративной сети: его видно на зеркалируемом порту,
# в логах прокси и с любого хоста в том же сегменте. Поэтому боевой адрес
# обязан быть https, а исключение делается явно (см. LLM_ALLOW_INSECURE).
# Корневой сертификат внутреннего УЦ, которым подписан сертификат сервера
# модели, — если его нет в системном хранилище
LLM_CA_FILE = os.getenv("LLM_CA_FILE", "").strip()
# Осознанный отказ от TLS: закрытый сегмент, отладка, туннель снаружи.
# Именно осознанный — по умолчанию демон не стартует с http-адресом
LLM_ALLOW_INSECURE = _bool("LLM_ALLOW_INSECURE", False)

# --- Поведение демона ---
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", 20))
MARK_SEEN = _bool("MARK_SEEN", True)
DRY_RUN = _bool("DRY_RUN", False)
# Сколько писем обрабатывается одновременно. Потолок задаёт ключ --max-num-seqs
# сервера vLLM: сверх него запросы встают в его собственную очередь, и клиентский
# параллелизм перестаёт что-либо давать.
WORKERS = max(1, int(os.getenv("WORKERS", 1)))

# --- Доступ и лимиты ---
ALLOWED_SENDERS: List[str] = [
    addr.strip().lower() for addr in os.getenv("ALLOWED_SENDERS", "").split(",") if addr.strip()
]
# Разрешать ли записи вида `@company.ru`, открывающие домен целиком.
# По умолчанию нет: сервисом пользуются несколько должностных лиц, а доменная
# запись пускает любого сотрудника — включая того, кому переслали чужой тред.
# Ширина доступа здесь прямо задаёт число людей, способных добраться до чужой
# переписки, поэтому расширение делается явным решением, а не умолчанием.
ALLOW_DOMAIN_WILDCARD = _bool("ALLOW_DOMAIN_WILDCARD", False)
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", 20000))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", 40))
# Потолок на всё, что уезжает в запрос: история сессии плюс текущее письмо,
# в символах. Раньше историю резали по окну модели (LLM_NUM_CTX), но окно —
# параметр самой модели, и живёт он теперь на sofi-mail в Open WebUI; клиенту
# оно не видно. Это не настройка модели, а страховка от запроса, который сервер
# отвергнет целиком: с ней теряются самые старые реплики, без неё — всё письмо.
# Ориентир — окно модели в токенах, умноженное на 3 (символов на токен для
# русского), с запасом на ответ и на то, что подмешают знания и фильтры.
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", 60000))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", 20))

# --- Вложения ---------------------------------------------------------------
# Документ из письма разбирается локально (unstructured), а его текст уезжает
# файлом в Open WebUI и живёт там до срока хранения ниже. Это данные той же
# природы, что и переписка, но лежат они уже не только в нашей базе, поэтому
# выключатель здесь общий и явный: ATTACHMENTS_ENABLED=false — вложения
# игнорируются, пользователю приходит ответ по тексту письма с примечанием.
ATTACHMENTS_ENABLED = _bool("ATTACHMENTS_ENABLED", True)
# Потолок на один файл. Не про диск, а про время: 20 МБ pdf разбираются
# секундами, а сотня мегабайт держала бы поток обработки минуту
ATTACHMENT_MAX_MB = int(os.getenv("ATTACHMENT_MAX_MB", 20))
# Сколько файлов берём из одного письма. Остальные пропускаются с примечанием
ATTACHMENT_MAX_COUNT = int(os.getenv("ATTACHMENT_MAX_COUNT", 5))
# Сколько файлов сессии уходит в запрос вместе с текущим письмом. Файлы
# накапливаются тредом, и без потолка на двадцатом письме в запрос уехала бы
# вся папка собеседника
ATTACHMENT_MAX_SESSION_FILES = int(os.getenv("ATTACHMENT_MAX_SESSION_FILES", 10))
# Граница «грузим целиком»: до этого числа страниц документ уходит с
# context=full и модель видит его текст полностью, дальше — фокусированный
# поиск Open WebUI плюс оглавление в тексте запроса (см. src/attachments.py)
ATTACHMENT_FULL_CONTEXT_PAGES = int(os.getenv("ATTACHMENT_FULL_CONTEXT_PAGES", 20))
# Та же граница, но по объёму: сколько символов извлечённого текста вправе
# уйти в модель целиком. Страница — мера вёрстки, а не текста: двадцать
# страниц регламента и двадцать страниц выгрузки различаются по объёму на
# порядок, и у форматов без страниц (docx, xlsx, txt) страницы вообще
# считаются из этого же текста. Порог берётся по тексту, а не по весу файла
# в письме: вес держит ATTACHMENT_MAX_MB, а к объёму текста он отношения
# не имеет — скан на 2 МБ несёт одну страницу, docx на 200 КБ — пятьсот.
# Документ уходит целиком, только если проходит оба порога; не прошёл любой —
# дальше как с длинным: фокусированный поиск и та же проверка оглавления.
# Ориентир для значения — ATTACHMENT_FULL_CONTEXT_PAGES страниц по 1800
# знаков (машинописная страница, см. CHARS_PER_PAGE в src/attachments.py)
ATTACHMENT_FULL_CONTEXT_CHARS = int(os.getenv("ATTACHMENT_FULL_CONTEXT_CHARS", 40000))
# Фокусированный поиск ведёт оглавление, собранное из заголовков документа, и
# без него за порогом страниц модель видит случайные фрагменты. Здесь — сколько
# страниц вправе приходиться на один заголовок, чтобы оглавление считалось
# картой документа: у файла на 200 страниц с тремя заголовками оглавление есть
# формально, а вести по нему поиск не выйдет. Документ, не прошедший проверку,
# в работу не берётся вовсе — пользователю уходит объяснение и что делать
# дальше, а не ответ по случайной трети текста (см. src/attachments.py)
ATTACHMENT_OUTLINE_PAGES_PER_ENTRY = int(os.getenv("ATTACHMENT_OUTLINE_PAGES_PER_ENTRY", 10))
# Потолок на документ целиком, в символах извлечённого текста: выше него
# документ не берётся в работу вообще, ни целиком, ни поиском. Он не про
# контекст модели (для этого выше два порога), а про то, что дальше нашей
# защиты нет: требование к оглавлению растёт вместе с документом, но упирается
# в потолок самого оглавления (60 строк, MAX_OUTLINE_ENTRIES) примерно на
# миллионе знаков — и документ любого размера с шестьюдесятью заголовками
# проходил бы проверку. Отсюда и значение по умолчанию: ровно та отметка,
# где проверка оглавления перестаёт расти (~550 машинописных страниц).
# ATTACHMENT_MAX_MB здесь не помощник: он про вес файла, а вес и объём текста
# связаны слабо — в 20 МБ docx помещаются сотни миллионов знаков.
# 0 — не ограничивать
ATTACHMENT_MAX_CHARS = int(os.getenv("ATTACHMENT_MAX_CHARS", 1_000_000))
# Сколько ждать, пока Open WebUI разберёт и проиндексирует загруженный файл
ATTACHMENT_PROCESS_TIMEOUT_SEC = int(os.getenv("ATTACHMENT_PROCESS_TIMEOUT_SEC", 120))
# Срок жизни файла в Open WebUI, дней. 0 — не удалять (файлы копятся, и удалять
# их придётся руками). Свой, отдельный от RETENTION_DAYS: в Open WebUI файлы
# лежат под общей сервисной учёткой, то есть в более широком доступе, чем
# наша база, и держать их дольше переписки нет причин
ATTACHMENT_RETENTION_DAYS = int(os.getenv("ATTACHMENT_RETENTION_DAYS", 30))

# --- Сессии и хранилище ---
DB_PATH = _path("DB_PATH", "data/sessions.db")
THREAD_BY_SUBJECT = _bool("THREAD_BY_SUBJECT", True)
# Хранить ли тело письма до отсечения цитаты. Внутри цитаты едет вся прежняя
# переписка треда, включая реплики третьих лиц, которые сервису не писали —
# в базе оказываются данные людей, не имеющих к ней отношения. Поле нужно
# только чтобы разглядеть промах эвристики цитат (`history --raw`), поэтому
# по умолчанию выключено: принцип минимизации важнее удобства отладки.
STORE_RAW_BODY = _bool("STORE_RAW_BODY", False)
# Сколько дней хранить переписку. 0 — хранить бессрочно (прежнее поведение).
# Бессрочное хранение означает, что ущерб от компрометации базы растёт вечно,
# а удалить свои данные по требованию нечем.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", 90))

# --- Журналирование ---
# Писать ли в лог адреса и темы писем как есть. Тема письма должностного лица
# сама по себе чувствительна («Re: сокращение отдела продаж»), а лог уходит
# в journald, доступный группе systemd-journal и системе сбора логов.
# По умолчанию адрес маскируется, тема не пишется вовсе; включать только
# на время разбора конкретной проблемы.
LOG_PII = _bool("LOG_PII", False)


def is_sender_allowed(address: str) -> bool:
    """Проверка адреса по whitelist.

    Запись вида `@company.ru` разрешает весь домен, но срабатывает только при
    ALLOW_DOMAIN_WILDCARD: `validate` такую пару отвергает на старте, а проверка
    здесь дублирует её на случай, когда конфигурация собрана в обход validate.
    """
    address = address.lower().strip()
    if not address or "@" not in address:
        return False
    if address in ALLOWED_SENDERS:
        return True
    if not ALLOW_DOMAIN_WILDCARD:
        return False
    domain = "@" + address.split("@")[-1]
    return domain in ALLOWED_SENDERS


def _oauth2_problems() -> List[str]:
    """Проверки, осмысленные только при EWS_AUTH=oauth2.

    Entra ID на неполный набор параметров отвечает кодом вида AADSTS900023 без
    внятного текста, поэтому дешевле поймать это на старте.
    """
    if EWS_AUTH != "oauth2":
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


def _llm_problems() -> List[str]:
    """Проверки шлюза к модели, ловящие частую путаницу в адресе."""
    problems = []
    if not LLM_BASE_URL:
        problems.append(
            "LLM_BASE_URL не задан: модель отдаёт Open WebUI компании, адрес "
            "нужно указать явно (https://sofi.cdu.so/api)"
        )
    # Хвост /v1 остался в конфигурациях с тех пор, когда демон ходил прямо
    # в vLLM. У Open WebUI по этому пути внутренний REST (чаты, знания,
    # /api/v1/chats/new), генерации там нет вовсе — запрос упёрся бы в 404
    # или, того хуже, в чужой обработчик
    if LLM_BASE_URL.endswith("/v1"):
        problems.append(
            f"LLM_BASE_URL={LLM_BASE_URL} заканчивается на /v1 — это адрес vLLM. "
            "Open WebUI принимает генерацию по /api (POST /api/chat/completions), "
            "а /api/v1 — его внутренний REST. Уберите суффикс: "
            "https://sofi.cdu.so/api"
        )
    if not LLM_API_KEY:
        problems.append(
            "LLM_API_KEY не задан: ключ API сервисной учётной записи Open WebUI "
            "(Settings -> Account -> API keys). Без него сервер отвечает 401 — "
            "в отличие от vLLM, где ключ был необязателен"
        )
    return problems


def _transport_problems() -> List[str]:
    """Проверки, закрывающие открытую передачу переписки по сети."""
    problems = []
    if LLM_BASE_URL and not LLM_BASE_URL.startswith("https://") and not LLM_ALLOW_INSECURE:
        problems.append(
            f"LLM_BASE_URL={LLM_BASE_URL} — открытый http. В запросе к модели уходит "
            "полный текст письма и история переписки, по http это открытый текст "
            "в корпоративной сети. Укажите https-адрес (корневой сертификат внутреннего "
            "УЦ — в LLM_CA_FILE) либо, если сегмент изолирован, разрешите это явно: "
            "LLM_ALLOW_INSECURE=true"
        )
    if LLM_CA_FILE and not Path(LLM_CA_FILE).exists():
        problems.append(f"LLM_CA_FILE={LLM_CA_FILE}: файл не найден")
    if not MAIL_TLS_VERIFY:
        problems.append(
            "MAIL_TLS_VERIFY=false отключает проверку сертификата Exchange — "
            "почта уязвима к перехвату. Это режим отладки, не для боевого контура: "
            "укажите корневой сертификат внутреннего УЦ в MAIL_CA_FILE"
        )
    return problems


def _access_problems() -> List[str]:
    """Проверки ширины доступа к сервису."""
    wildcards = [addr for addr in ALLOWED_SENDERS if addr.startswith("@")]
    if wildcards and not ALLOW_DOMAIN_WILDCARD:
        return [
            f"ALLOWED_SENDERS содержит запись на весь домен ({', '.join(wildcards)}). "
            "Домен целиком пускает любого сотрудника — в том числе того, кому переслали "
            "чужой тред. Перечислите конкретные адреса или подтвердите решение явно: "
            "ALLOW_DOMAIN_WILDCARD=true"
        ]
    return []


def validate() -> None:
    """Проверка настроек, без которых демон не имеет смысла.

    Вызывается в начале команд, работающих с почтой. Ошибка на старте понятнее,
    чем падение в середине обработки письма: пустой ALLOWED_SENDERS, например,
    привёл бы к тому, что демон молча игнорирует вообще все входящие.
    """
    problems = []
    if not MAIL_ADDRESS:
        problems.append("MAIL_ADDRESS не задан")
    # Пароль не нужен там, где аутентификация идёт не по нему: Kerberos берёт
    # билет из кеша, а OAuth2 без пароля работает от имени приложения
    password_optional = EWS_AUTH in ("gssapi", "sspi", "oauth2")
    if not MAIL_PASSWORD and not password_optional:
        problems.append(
            "MAIL_PASSWORD не задан (пароль служебной учётной записи домена). "
            "Без пароля работают только EWS_AUTH=gssapi, sspi и oauth2"
        )
    problems.extend(_oauth2_problems())
    problems.extend(_llm_problems())
    if MAIL_CA_FILE and not Path(MAIL_CA_FILE).exists():
        problems.append(f"MAIL_CA_FILE={MAIL_CA_FILE}: файл не найден")
    problems.extend(_transport_problems())
    problems.extend(_access_problems())
    if not ALLOWED_SENDERS:
        problems.append(
            "ALLOWED_SENDERS пуст — демон отвечал бы никому. "
            "Укажите хотя бы свой адрес."
        )
    if problems:
        raise ValueError("Ошибки конфигурации (.env):\n  - " + "\n  - ".join(problems))
