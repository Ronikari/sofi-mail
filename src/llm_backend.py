# http-шлюз к Open WebUI: генерация ответа и опрос списка моделей.
# порядок: адрес из LLM_BASE_URL -> дописывание суффикса /api -> сборка клиента
# ChatOpenAI с ssl-контекстом -> вызов POST /api/chat/completions -> текст ответа.
# вход: список сообщений langchain из llm.py и ссылки на файлы из owui_files.py.
# выход: строка ответа модели; describe() отдаёт строку состояния для команды check.
# LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT_SEC и LLM_CA_FILE
# импортируются из config.py.
# вызывается из llm.py; функции ssl_context, auth_headers и webui_url
# использует owui_files.py.
#
# генерация выполняется по адресу {base}/api/chat/completions в формате,
# совместимом с openai. путь /api/v1/... занят внутренним rest интерфейса
# (чаты, знания, файлы), генерации по нему нет. адрес с хвостом /v1 остался
# от прямых обращений к vLLM и отвергается на старте в config._llm_problems.
#
# тело запроса содержит model, messages и files. температура, потолок ответа
# и системный промпт заданы на модели sofi-mail в Open WebUI; те же поля
# в запросе перекрывают настройку модели.
#
# чаты через /api/v1/chats/new не создаются: история переписки лежит в sqlite,
# где действуют срок хранения и удаление по требованию. под общей сервисной
# учётной записью такие чаты образуют один общий чат, доступный каждому
# владельцу её ключа

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


# выход: контекст проверки сертификата либо None при отсутствии LLM_CA_FILE
# и при адресе http; None означает проверку по умолчанию средствами клиента.
# один объект подходит httpx (параметр verify) и urllib (параметр context)
def ssl_context() -> Optional[ssl.SSLContext]:
    """Отдаёт ssl-контекст с корневым сертификатом внутреннего УЦ."""
    # условие отсекает два случая: сертификат подписан УЦ из системного
    # хранилища, адрес использует http и проверять нечего
    if not LLM_CA_FILE or not LLM_BASE_URL.startswith("https://"):
        return None

    # create_default_context добавляет корневой сертификат к политике проверки;
    # сверка имени хоста и цепочки продолжает действовать
    return ssl.create_default_context(cafile=LLM_CA_FILE)


# вход: адрес из LLM_BASE_URL, хвостовые слэши допустимы.
# выход: адрес, оканчивающийся на /api
def webui_url(raw: str) -> str:
    """Приводит адрес Open WebUI к форме с обязательным суффиксом /api."""
    url = raw.rstrip("/")

    # суффикс дописывается к адресу без него: клиент ChatOpenAI строит путь
    # {base}/chat/completions, и без /api сервер отвечает 404 внутри клиента
    return url if url.endswith("/api") else f"{url}/api"


# выход: словарь с заголовком Authorization; пустой словарь при пустом ключе.
# ключ принадлежит сервисной учётной записи, тем же ключом ходит owui_files.py
def auth_headers() -> Dict[str, str]:
    """Собирает заголовок авторизации с ключом сервисной учётной записи."""
    return {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}


# вход: url эндпоинта, заголовки запроса, what — имя сервиса для текста ошибки.
# выход: разобранный json.
# сетевые сбои приводятся к RuntimeError с текстом, называющим причину.
# побочный эффект: http-запрос
def _fetch_json(url: str, headers: Dict[str, str], what: str) -> Any:
    """Выполняет GET и возвращает разобранный json."""
    request = urllib.request.Request(url, headers=headers)
    try:
        # таймаут 5 секунд: функция обслуживает только команду check
        with urllib.request.urlopen(request, timeout=5, context=ssl_context()) as response:
            return json.load(response)

    # ветка http-кода 4xx и 5xx: сервер доступен и отказал
    except urllib.error.HTTPError as exc:
        # коды 401 и 403 означают просроченный ключ либо ключ другой учётной
        # записи; список моделей при этом приходит чужой либо пустой
        hint = " (проверьте LLM_API_KEY)" if exc.code in (401, 403) else ""
        raise RuntimeError(f"{what} ответил {exc.code} на {url}{hint}") from exc

    # ветка отказа соединения: разрешение имени, отказ порта, сбой проверки tls
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{what} недоступен на {url}: {exc.reason}") from exc


# вход: models — идентификаторы моделей из ответа сервера; where — адрес шлюза
# для текста результата.
# выход: строка «адрес, модель X».
# поднимает RuntimeError при отсутствии LLM_MODEL в списке
def _require_model(models: List[str], where: str) -> str:
    """Сверяет LLM_MODEL со списком моделей, доступных сервисной учётной записи."""
    # сравнение точным равенством: совпадение по префиксу направило бы запрос
    # к базовой модели, работающей без системного промпта, знаний и фильтров
    if LLM_MODEL not in models:
        raise RuntimeError(
            f"модель {LLM_MODEL} не найдена. Доступны: {', '.join(models) or 'нет моделей'}. "
            "Имя должно совпадать с Model ID модели рабочего пространства "
            "(Workspace -> Models), а сама модель — быть доступна учётной записи, "
            "чей ключ указан в LLM_API_KEY."
        )
    return f"{where}, модель {LLM_MODEL}"


# объект создаётся на каждый запрос функцией get_backend, разделяемое состояние
# в нём отсутствует
class OpenWebUIBackend:
    def __init__(self) -> None:
        # адрес приводится к форме с /api один раз при создании объекта
        self.url = webui_url(LLM_BASE_URL)

    # вход: messages — контекст из llm.build_messages; files — ссылки
    # на документы из owui_files.reference, пустая последовательность допустима.
    # выход: строка content из ответа модели.
    # побочный эффект: http-запрос длительностью до LLM_TIMEOUT_SEC секунд
    def complete(
        self, messages: Sequence[BaseMessage], files: Sequence[Dict[str, Any]] = ()
    ) -> str:
        """Отправляет контекст модели и возвращает текст её ответа."""
        # импорт по месту вызова: httpx и langchain_openai загружаются только
        # перед обращением к модели
        import httpx
        from langchain_openai import ChatOpenAI

        # отдельный http-клиент нужен при заданном LLM_CA_FILE: httpx проверяет
        # цепочку по набору certifi, корневой сертификат внутреннего УЦ
        # в нём отсутствует и запрос обрывается на проверке сертификата
        context = ssl_context()
        http_client = httpx.Client(verify=context, timeout=LLM_TIMEOUT_SEC) if context else None

        client = ChatOpenAI(
            base_url=self.url,
            api_key=LLM_API_KEY,
            model=LLM_MODEL,
            timeout=LLM_TIMEOUT_SEC,
            # ChatOpenAI выполняет 2 внутренние попытки по умолчанию; вместе
            # с pipeline._retry это давало бы шесть обращений на письмо,
            # внутренние идут без пауз и без записи в лог
            max_retries=0,
            http_client=http_client,
            # поле files отсутствует в openai-совместимом теле запроса, Open WebUI
            # ожидает его рядом с messages; extra_body добавляет поле напрямую.
            # значение None оставляет запрос без этого поля
            extra_body={"files": list(files)} if files else None,
        )
        try:
            # invoke выполняет запрос и возвращает объект сообщения, ответ лежит
            # в атрибуте content
            return str(client.invoke(list(messages)).content)
        finally:
            # клиент создан под этот запрос: закрытие освобождает соединения,
            # накапливающиеся по одному на письмо
            if http_client is not None:
                http_client.close()

    # выход: строка с адресом шлюза и именем модели для команды check.
    # побочный эффект: http-запрос к списку моделей
    def describe(self) -> str:
        """Проверяет доступность шлюза и наличие LLM_MODEL в списке моделей."""
        data = _fetch_json(f"{self.url}/models", auth_headers(), "Open WebUI")

        # ответ имеет форму {"data": [{"id": ...}, ...]}; отсутствующий id
        # заменяется пустой строкой и в список моделей не проходит
        return _require_model([m.get("id", "") for m in data.get("data", [])], self.url)


# выход: новый объект OpenWebUIBackend на каждый вызов.
# письма обрабатываются пулом потоков; объект без разделяемого состояния
# снимает требование потокобезопасности
def get_backend() -> OpenWebUIBackend:
    """Создаёт клиент шлюза для одного запроса."""
    return OpenWebUIBackend()
