"""Обращение к Open WebUI, которым пользуется `src/llm.py`.

Пайплайну и сборке контекста нужно от модели ровно две операции — сгенерировать
ответ на список сообщений и рассказать о себе для команды `check`. Всё
остальное (сборка контекста, отсечение рассуждений) живёт в `src/llm.py`
и от способа обращения к серверу не зависит — ровно как работа с почтой
отделена в `src/transport.py`.

Три особенности Open WebUI, которые видны в этом модуле:

- **Адрес.** Генерация идёт по `POST {base}/api/chat/completions`,
  OpenAI-совместимо. Путь `/api/v1/...` — это внутренний REST интерфейса
  (чаты, знания, файлы), и генерации там нет: адрес с хвостом `/v1`,
  оставшийся со времён прямого обращения к vLLM, отвергается на старте
  (см. `config._llm_problems`).

- **Запрос без параметров.** В теле уходят только `model`, `messages` и, если
  к письму были вложения, `files` — ссылки на уже загруженные документы
  (`src/owui_files.py`).
  Температура, потолок ответа и системный промпт заданы на модели sofi-mail
  в рабочем пространстве Open WebUI; переданные в запросе, они перекрыли бы
  настройку модели, и ответ разошёлся бы с тем, что администратор видит
  в интерфейсе. Чаты в Open WebUI демон тоже не заводит (`/api/v1/chats/new`):
  история переписки лежит в SQLite, где к ней есть срок хранения и удаление
  по требованию, — под общей сервисной учёткой в интерфейсе всё это выглядело
  бы как один общий чат, доступный каждому, у кого есть её ключ.

- **Собственные повторы.** У `ChatOpenAI` они включены по умолчанию (2 попытки),
  и вместе с `pipeline._retry` дали бы шесть обращений на одно письмо вместо
  трёх, причём внутренние — без пауз и без записи в лог. Отключены явно:
  политика повторов в проекте одна и живёт в пайплайне.

Клиент создаётся на каждый запрос, а не переиспользуется: письма обрабатываются
пулом потоков, и объект без разделяемого состояния снимает вопрос о его
потокобезопасности целиком.
"""

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.messages import BaseMessage

from src.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CA_FILE,
    LLM_MODEL,
    LLM_TIMEOUT_SEC,
)

log = logging.getLogger(__name__)


def ssl_context() -> Optional[ssl.SSLContext]:
    """Контекст проверки сертификата Open WebUI.

    None — сертификат подписан УЦ из системного хранилища (или адрес http, и
    проверять нечего); тогда клиенты используют свою проверку по умолчанию,
    и вмешиваться незачем.

    Контекст возвращается общий: httpx (`verify=`) и urllib (`context=`)
    принимают ssl.SSLContext одинаково, поэтому один и тот же внутренний УЦ
    работает и в запросах к модели, и в проверке из `check`. Проверка имени
    хоста и цепочки остаётся включённой — контекст здесь только добавляет
    доверенный корень, а не ослабляет политику.
    """
    if not LLM_CA_FILE or not LLM_BASE_URL.startswith("https://"):
        return None
    return ssl.create_default_context(cafile=LLM_CA_FILE)


def webui_url(raw: str) -> str:
    """Адрес OpenAI-совместимой точки входа Open WebUI — обязательно с `/api`.

    Дописывается, если его забыли: без него клиент соберёт путь вида
    `/chat/completions`, которого на сервере нет, и ошибка вылезет как 404
    в глубине клиента.
    """
    url = raw.rstrip("/")
    return url if url.endswith("/api") else f"{url}/api"


def auth_headers() -> Dict[str, str]:
    """Заголовок с ключом сервисной учётки. Пустой ключ отсекает `validate`.

    Публичная: тем же ключом ходит файловый API (`src/owui_files.py`) — это один
    и тот же сервер и одна и та же учётная запись.
    """
    return {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}


def _fetch_json(url: str, headers: Dict[str, str], what: str) -> Any:
    """GET с разбором JSON. Любая сетевая беда — понятный RuntimeError."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5, context=ssl_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 401 здесь — самая частая причина: ключ протух или заведён под другой
        # учёткой, и тогда список моделей приходит чужой либо не приходит вовсе
        hint = " (проверьте LLM_API_KEY)" if exc.code in (401, 403) else ""
        raise RuntimeError(f"{what} ответил {exc.code} на {url}{hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{what} недоступен на {url}: {exc.reason}") from exc


def _require_model(models: List[str], where: str) -> str:
    """Строгая сверка идентификатора модели со списком в Open WebUI.

    Точное равенство, а не вхождение: молчаливое совпадение по префиксу дало бы
    ответы не той модели, которую настраивали, — например базовой вместо
    sofi-mail, то есть без системного промпта, знаний и фильтров.
    """
    if LLM_MODEL not in models:
        raise RuntimeError(
            f"модель {LLM_MODEL} не найдена. Доступны: {', '.join(models) or 'нет моделей'}. "
            "Имя должно совпадать с Model ID модели рабочего пространства "
            "(Workspace -> Models), а сама модель — быть доступна учётной записи, "
            "чей ключ указан в LLM_API_KEY."
        )
    return f"{where}, модель {LLM_MODEL}"


class OpenWebUIBackend:
    """Шлюз к модели: Open WebUI компании, OpenAI-совместимый API."""

    def __init__(self) -> None:
        self.url = webui_url(LLM_BASE_URL)

    def complete(
        self, messages: Sequence[BaseMessage], files: Sequence[Dict[str, Any]] = ()
    ) -> str:
        import httpx
        from langchain_openai import ChatOpenAI

        # Свой http-клиент нужен только под внутренний УЦ: по умолчанию httpx
        # проверяет цепочку по certifi, где корпоративного корня нет,
        # и запрос упал бы на проверке сертификата
        context = ssl_context()
        http_client = httpx.Client(verify=context, timeout=LLM_TIMEOUT_SEC) if context else None

        # Ни temperature, ни max_tokens: они заданы на модели sofi-mail
        # в Open WebUI, и переданные в запросе перекрыли бы её настройку
        # files уезжают через extra_body: в OpenAI-совместимом теле запроса
        # такого поля нет, а Open WebUI ждёт его рядом с messages. Пустой список
        # не отправляется вовсе — лишнее поле в запросе к модели без вложений
        client = ChatOpenAI(
            base_url=self.url,
            api_key=LLM_API_KEY,
            model=LLM_MODEL,
            timeout=LLM_TIMEOUT_SEC,
            max_retries=0,  # повторы — забота pipeline._retry, см. шапку модуля
            http_client=http_client,
            extra_body={"files": list(files)} if files else None,
        )
        try:
            return str(client.invoke(list(messages)).content)
        finally:
            if http_client is not None:
                http_client.close()

    def describe(self) -> str:
        """Строка о состоянии шлюза — для команды `check`.

        Недоступность, отказ по ключу и отсутствие нужной модели поднимают
        RuntimeError с текстом, по которому понятно, что чинить.
        """
        data = _fetch_json(f"{self.url}/models", auth_headers(), "Open WebUI")
        return _require_model([m.get("id", "") for m in data.get("data", [])], self.url)


def get_backend() -> OpenWebUIBackend:
    """Клиент шлюза — новый на каждый запрос, см. шапку модуля."""
    return OpenWebUIBackend()
