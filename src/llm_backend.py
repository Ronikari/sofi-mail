# http-шлюз к Open WebUI: Path A (создать чат -> опросить задачу -> прочитать ответ из чата ->
# удалить чат) — единственный режим со встроенными серверными тулами; см. AI/OWUI/README.md.

# /api/v1/... — внутренний rest (чаты/файлы), им читается и удаляется чат; хвост /v1 в
# LLM_BASE_URL — другой адрес (vLLM), отвергается на старте в config._llm_problems.

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import AIMessage, BaseMessage

from src.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CA_FILE,
    LLM_DELETE_CHAT,
    LLM_MODEL,
    LLM_TIMEOUT_SEC,
    LLM_TOOL_IDS,
)

log = logging.getLogger(__name__)

# пауза между опросами задачи генерации: короткий ответ готов за секунды, поэтому опрос
# начинается часто, а удваивается только при долгой генерации (тулы, большой документ).
_POLL_START_SEC = 0.5
_POLL_MAX_SEC = 5.0


# None означает проверку по умолчанию средствами клиента; один объект подходит
# httpx (параметр verify) и urllib (параметр context).
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

    # суффикс дописывается к адресу без него: без него POST /chat/completions
    # уходит по пути, которого на сервере нет
    return url if url.endswith("/api") else f"{url}/api"


# выход: словарь с заголовком Authorization; пустой словарь при пустом ключе.
# ключ принадлежит сервисной учётной записи, тем же ключом ходит owui_files.py
def auth_headers() -> Dict[str, str]:
    """Собирает заголовок авторизации с ключом сервисной учётной записи."""
    return {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}


# сетевые сбои приводятся к RuntimeError с текстом, называющим причину.
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


# поднимает RuntimeError при отсутствии LLM_MODEL в списке моделей.
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


class LLMError(RuntimeError):
    """Отказ на любом шаге обращения к Open WebUI при генерации ответа."""


# вход: response — ответ httpx; what — описание операции для текста ошибки.
# поднимает LLMError при коде 400 и выше, при меньшем коде возвращает None
def _raise_for(response, what: str) -> None:
    """Переводит http-ошибку Open WebUI в LLMError с текстом причины."""
    if response.status_code < 400:
        return

    hint = " (проверьте LLM_API_KEY)" if response.status_code in (401, 403) else ""
    raise LLMError(
        f"{what}: Open WebUI ответил {response.status_code}{hint} — {response.text[:300]}"
    )


# короткая квитанция Path A иногда приходит завёрнутой в SSE даже без реального потока
# чанков — запасная ветка разбирает первую строку data:, если json не читается напрямую.
def _response_json(response, what: str) -> Dict[str, Any]:
    """Разбирает тело ответа как json, с запасным разбором SSE-обёртки."""
    text = response.text.strip()
    try:
        return dict(json.loads(text))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            return dict(json.loads(payload))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    raise LLMError(f"{what}: ответ не разобрать как JSON ({text[:200]!r})")


# в списке встречаются только HumanMessage и AIMessage — llm.py не создаёт системных
# сообщений, системный промпт задан на модели в Open WebUI.
def _to_openai_messages(messages: Sequence[BaseMessage]) -> List[Dict[str, Any]]:
    """Переводит сообщения langchain в формат сообщений OpenAI."""
    return [
        {
            "role": "assistant" if isinstance(m, AIMessage) else "user",
            # convert_url_images_to_base64 в Open WebUI 0.8.12 подставляет картинку по id только в content-список
            "content": m.content if isinstance(m.content, list) else str(m.content),
        }
        for m in messages
    ]


def _text_of(content: Any) -> str:
    """Достаёт текст из содержимого сообщения: строки или списка частей."""
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


# Open WebUI 0.11 заменяет messages цепочкой из базы чата и вставляет картинку в реплику
# только из files с type=image и url; id файла в url сервер разрешает в base64.
def _image_files(content: Any) -> List[Dict[str, Any]]:
    """Собирает ссылки на картинки из частей image_url для реплики чата."""
    if not isinstance(content, list):
        return []
    return [
        {"type": "image", "id": part["image_url"]["url"], "url": part["image_url"]["url"]}
        for part in content
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


# объект создаётся на каждый запрос функцией get_backend, разделяемое состояние
# в нём отсутствует
class OpenWebUIBackend:
    def __init__(self) -> None:
        # адрес приводится к форме с /api один раз при создании объекта
        self.url = webui_url(LLM_BASE_URL)

    # побочный эффект: http-запросы длительностью до LLM_TIMEOUT_SEC секунд,
    # создание и удаление одного чата в Open WebUI.
    def complete(
        self, messages: Sequence[BaseMessage], files: Sequence[Dict[str, Any]] = ()
    ) -> str:
        """Отправляет контекст модели через Path A и возвращает текст её ответа."""
        import httpx

        # отдельный http-клиент нужен при заданном LLM_CA_FILE: httpx проверяет цепочку по
        # набору certifi, где корневого сертификата внутреннего УЦ нет, и запрос обрывается.
        context = ssl_context()
        client = httpx.Client(
            base_url=self.url,
            headers=auth_headers(),
            verify=context if context else True,
            timeout=LLM_TIMEOUT_SEC,
        )

        chat_id: Optional[str] = None
        try:
            chat_id, message_id = self._start(client, messages, files)
            self._wait_task(client, chat_id)
            return self._read_answer(client, chat_id, message_id)
        finally:
            if chat_id and LLM_DELETE_CHAT:
                self._delete_chat(client, chat_id)
            client.close()

    # POST создаёт новый чат в Open WebUI и форкает фоновую задачу генерации;
    # тело ответа здесь — короткая квитанция, не текст ответа.
    def _start(
        self, client, messages: Sequence[BaseMessage], files: Sequence[Dict[str, Any]]
    ) -> Tuple[str, str]:
        """Запускает генерацию и отдаёт chat_id вместе с id будущего ответа."""
        openai_messages = _to_openai_messages(messages)
        assistant_message_id = str(uuid.uuid4())

        # user_message — последняя реплика того же списка: build_messages
        # в llm.py всегда кладёт текущее письмо последним элементом
        user_message: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "role": "user",
            # реплика чата хранит текст строкой; картинки к ней Open WebUI берёт из files
            "content": _text_of(openai_messages[-1]["content"]) if openai_messages else "",
        }

        body: Dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": openai_messages,
            "stream": True,
            # parent_id=None и отсутствующий chat_id — признак нового чата для
            # Open WebUI: сервер сам генерирует chat_id и создаёт чат
            "parent_id": None,
            "id": assistant_message_id,
            # произвольная непустая строка: живой Socket.IO не нужен — без него
            # недоступен только один тул, code_interpreter на движке pyodide.
            "session_id": str(uuid.uuid4()),
            "user_message": user_message,
        }
        images = _image_files(openai_messages[-1]["content"]) if openai_messages else []
        if files:
            body["files"] = list(files)
        if files or images:
            user_message["files"] = [*files, *images]
        if LLM_TOOL_IDS:
            body["tool_ids"] = list(LLM_TOOL_IDS)

        response = client.post("/chat/completions", json=body)
        _raise_for(response, "запрос к модели")
        payload = _response_json(response, "запрос к модели")

        chat_id = payload.get("chat_id")
        if not chat_id:
            raise LLMError(f"запрос к модели: в ответе нет chat_id ({str(payload)[:300]})")

        return str(chat_id), assistant_message_id

    # опрашивает список фоновых задач чата, пока он не опустеет — так Open WebUI
    # сигнализирует конец генерации; поднимает LLMError по истечении LLM_TIMEOUT_SEC.
    def _wait_task(self, client, chat_id: str) -> None:
        """Ждёт завершения фоновой задачи генерации для chat_id."""
        deadline = time.monotonic() + LLM_TIMEOUT_SEC
        delay = _POLL_START_SEC

        while True:
            response = client.get(f"/tasks/chat/{chat_id}")
            _raise_for(response, "статус генерации")
            task_ids = _response_json(response, "статус генерации").get("task_ids") or []

            if not task_ids:
                return

            if time.monotonic() >= deadline:
                raise LLMError(
                    f"модель не ответила за {LLM_TIMEOUT_SEC} с (задача ещё выполняется)"
                )

            time.sleep(delay)
            # пауза удваивается до предела _POLL_MAX_SEC секунд
            delay = min(delay * 2, _POLL_MAX_SEC)

    # чат читается через /v1/... (внутренний rest, не /chat/completions): финальный
    # текст лежит в чате, а не в ответе на POST.
    def _read_answer(self, client, chat_id: str, message_id: str) -> str:
        """Достаёт текст ответа модели из истории чата."""
        response = client.get(f"/v1/chats/{chat_id}")
        _raise_for(response, "чтение ответа")
        payload = _response_json(response, "чтение ответа")

        history_messages = (payload.get("chat") or {}).get("history", {}).get("messages", {})
        message = history_messages.get(message_id)

        if message is None:
            raise LLMError(f"чат {chat_id}: реплика {message_id} не найдена в истории")

        error = message.get("error")
        if error:
            raise LLMError(f"модель вернула ошибку: {str(error)[:300]}")

        return str(message.get("content") or "")

    # отказ удаления не прерывает ответ пользователю: письмо уже прочитано из чата,
    # а сам чат остаётся в Open WebUI до следующей уборки — на текст письма и sqlite не влияет.
    def _delete_chat(self, client, chat_id: str) -> None:
        """Удаляет чат, отправляя письмо; отказ только пишет предупреждение в лог."""
        try:
            response = client.delete(f"/v1/chats/{chat_id}")
            if response.status_code >= 400 and response.status_code != 404:
                log.warning(
                    "чат %s не удалён в Open WebUI: %s %s",
                    chat_id, response.status_code, response.text[:120],
                )
        except Exception as exc:
            log.warning("чат %s не удалён в Open WebUI: %s", chat_id, exc)

    # выход: строка с адресом шлюза и именем модели для команды check.
    # побочный эффект: http-запрос к списку моделей
    def describe(self) -> str:
        """Проверяет доступность шлюза и наличие LLM_MODEL в списке моделей."""
        data = _fetch_json(f"{self.url}/models", auth_headers(), "Open WebUI")

        # ответ имеет форму {"data": [{"id": ...}, ...]}; отсутствующий id
        # заменяется пустой строкой и в список моделей не проходит
        return _require_model([m.get("id", "") for m in data.get("data", [])], self.url)


# письма обрабатываются пулом потоков; объект без разделяемого состояния
# снимает требование потокобезопасности.
def get_backend() -> OpenWebUIBackend:
    """Создаёт клиент шлюза для одного запроса."""
    return OpenWebUIBackend()
