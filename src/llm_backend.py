"""Обращение к серверу инференса (vLLM), которым пользуется `src/llm.py`.

Пайплайну и сборке контекста нужно от модели ровно две операции — сгенерировать
ответ на список сообщений и рассказать о себе для команды `check`. Всё
остальное (системный промпт, бюджет окна, отсечение рассуждений) живёт
в `src/llm.py` и от способа обращения к серверу не зависит — ровно как работа
с почтой отделена в `src/transport.py`.

Две особенности vLLM, которые видны в этом модуле:

- **Длина контекста.** Задаётся ключом `--max-model-len` при старте сервера
  и в запросе не участвует вообще. `LLM_NUM_CTX` — это клиентское знание о том,
  с чем сервер запущен; оно нужно `build_messages`, чтобы обрезать историю,
  и должно совпадать с реальным `--max-model-len`, иначе сервер отвергнет
  длинный запрос.
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
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SEC,
)

log = logging.getLogger(__name__)


def ssl_context() -> Optional[ssl.SSLContext]:
    """Контекст проверки сертификата сервера модели.

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


def vllm_url(raw: str) -> str:
    """Адрес OpenAI-совместимой точки входа vLLM — обязательно с `/v1`.

    Дописывается, если его забыли: без него клиент соберёт путь вида
    `/chat/completions`, которого на сервере нет, и ошибка вылезет как 404
    в глубине клиента.
    """
    url = raw.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def _fetch_json(url: str, headers: Dict[str, str], what: str) -> Any:
    """GET с разбором JSON. Любая сетевая беда — понятный RuntimeError."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5, context=ssl_context()) as response:
            return json.load(response)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{what} недоступен на {url}: {exc.reason}") from exc


def _require_model(models: List[str], where: str) -> str:
    """Строгая сверка имени модели со списком на сервере.

    Точное равенство, а не вхождение: молчаливое совпадение по префиксу дало бы
    ответы не той модели, которую настраивали.
    """
    if LLM_MODEL not in models:
        raise RuntimeError(
            f"модель {LLM_MODEL} не найдена. Доступны: {', '.join(models) or 'нет моделей'}. "
            "Имя должно совпадать с тем, под которым модель отдаётся сервером "
            "(путь репозитория либо ключ --served-model-name)."
        )
    return f"{where}, модель {LLM_MODEL}"


class VLLMBackend:
    """Сервер инференса компании: vLLM, OpenAI-совместимый API."""

    def __init__(self) -> None:
        self.url = vllm_url(LLM_BASE_URL)

    def complete(self, messages: Sequence[BaseMessage]) -> str:
        import httpx
        from langchain_openai import ChatOpenAI

        # Свой http-клиент нужен только под внутренний УЦ: по умолчанию httpx
        # проверяет цепочку по certifi, где корпоративного корня нет,
        # и запрос упал бы на проверке сертификата
        context = ssl_context()
        http_client = httpx.Client(verify=context, timeout=LLM_TIMEOUT_SEC) if context else None

        client = ChatOpenAI(
            base_url=self.url,
            # vLLM без ключа --api-key принимает любой токен, но пустую строку
            # клиент не пропустит, поэтому подставляем заглушку
            api_key=LLM_API_KEY or "EMPTY",
            model=LLM_MODEL,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT_SEC,
            max_retries=0,  # повторы — забота pipeline._retry, см. шапку модуля
            http_client=http_client,
        )
        try:
            return str(client.invoke(list(messages)).content)
        finally:
            if http_client is not None:
                http_client.close()

    def describe(self) -> str:
        """Строка о состоянии сервера — для команды `check`.

        Недоступность и отсутствие нужной модели поднимают RuntimeError
        с текстом, по которому понятно, что чинить.
        """
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
        data = _fetch_json(f"{self.url}/models", headers, "vLLM")
        return _require_model([m.get("id", "") for m in data.get("data", [])], self.url)


def get_backend() -> VLLMBackend:
    """Клиент сервера инференса — новый на каждый запрос, см. шапку модуля."""
    return VLLMBackend()
