"""Обращение к Open WebUI: нормализация адреса, Path A и разбор ответа модели.

Первая половина — llm.generate поверх заглушки backend (нормализация `/api`, рассуждения,
пустой ответ); вторая — сам цикл Path A в OpenWebUIBackend с заглушкой httpx.Client вместо сети.
"""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import llm, llm_backend
from src.llm_backend import LLMError, OpenWebUIBackend, webui_url, _response_json, _to_openai_messages


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://sofi.company.ru/api", "https://sofi.company.ru/api"),
        ("https://sofi.company.ru/api/", "https://sofi.company.ru/api"),
        # забытый суффикс дописывается: без него клиент собрал бы путь,
        # которого на сервере нет
        ("https://sofi.company.ru", "https://sofi.company.ru/api"),
        ("https://sofi.company.ru/", "https://sofi.company.ru/api"),
    ],
)
def test_webui_url_adds_api(raw, expected):
    assert webui_url(raw) == expected


class FakeBackend:
    """Бэкенд-заглушка: запоминает контекст и отдаёт заготовленный ответ."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.messages = None
        self.files = None

    def complete(self, messages, files=()):
        self.messages = list(messages)
        self.files = list(files)
        return self.answer

    def describe(self) -> str:
        return "fake"


@pytest.fixture
def fake_backend(monkeypatch):
    def install(answer: str) -> FakeBackend:
        backend = FakeBackend(answer)
        monkeypatch.setattr(llm, "get_backend", lambda: backend)
        return backend

    return install


def test_generate_strips_reasoning(fake_backend):
    """Рассуждения не должны уехать пользователю письмом.

    Рассуждающая модель без настроенного разбора отдаёт теги <think> прямо в content.
    """
    fake_backend("<think>надо подумать</think>\nОтвет по существу.")

    assert llm.generate([], "вопрос") == "Ответ по существу."


def test_generate_rejects_empty_answer(fake_backend):
    """Пустой ответ — ошибка, а не письмо в пустоту.

    Пайплайн переведёт письмо в error и его можно будет вернуть retry, а не молча ничего не ответить.
    """
    fake_backend("<think>рассуждал, пока не кончился лимит</think>")

    with pytest.raises(RuntimeError, match="пустой ответ"):
        llm.generate([], "вопрос")


def test_generate_sends_no_system_prompt(fake_backend):
    """Системный промпт задан на модели sofi-mail в Open WebUI, не здесь.

    Свой SystemMessage в запросе тихо переопределил бы часть правил модели, поэтому уезжают только реплики.
    """
    backend = fake_backend("ответ")

    llm.generate([], "как дела")

    roles = [type(m).__name__ for m in backend.messages]
    assert roles == ["HumanMessage"]
    assert backend.messages[-1].content == "как дела"


def test_generate_passes_file_references(fake_backend):
    """Ссылки на вложения уезжают рядом с сообщениями, а не в тексте вопроса."""
    backend = fake_backend("ответ")
    links = [{"type": "file", "id": "f-1", "context": "full"}]

    llm.generate([], "что в документе?", links)

    assert backend.files == links
    assert backend.messages[-1].content == "что в документе?"


# --- Path A: формат сообщений и разбор ответа --------------------------------


def test_to_openai_messages_maps_roles():
    messages = [HumanMessage(content="вопрос"), AIMessage(content="ответ")]

    assert _to_openai_messages(messages) == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ]


def test_to_openai_messages_keeps_image_parts_as_list():
    """Части image_url уходят списком: строка с repr python картинку до модели не доносит."""
    parts = [
        {"type": "text", "text": "что на картинке?"},
        {"type": "image_url", "image_url": {"url": "img-1"}},
    ]

    assert _to_openai_messages([HumanMessage(content=parts)]) == [
        {"role": "user", "content": parts},
    ]


def test_response_json_parses_plain_body():
    response = httpx.Response(200, text=json.dumps({"chat_id": "c1"}))

    assert _response_json(response, "тест") == {"chat_id": "c1"}


def test_response_json_falls_back_to_sse_data_line():
    """Короткая квитанция Path A иногда приходит завёрнутой в SSE."""
    body = 'event: message\ndata: {"chat_id": "c1", "task_ids": ["t1"]}\n\ndata: [DONE]\n'
    response = httpx.Response(200, text=body)

    assert _response_json(response, "тест") == {"chat_id": "c1", "task_ids": ["t1"]}


def test_response_json_raises_on_unreadable_body():
    response = httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(LLMError, match="не разобрать как JSON"):
        _response_json(response, "тест")


# --- Path A: полный цикл через заглушку httpx.Client -------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, body: dict = None):
        self.status_code = status_code
        self.text = json.dumps(body or {})


class FakeHttpClient:
    """Подменяет httpx.Client: путь -> очередь заготовленных ответов.

    calls хранит (метод, путь, тело) каждого вызова — по нему тесты сверяют, что ушло в запрос.
    """

    def __init__(self, responses: dict):
        self._responses = {k: list(v) if isinstance(v, list) else [v] for k, v in responses.items()}
        self.calls = []
        self.closed = False

    # очередь с одним элементом отдаёт его повторно на каждый вызов: чтобы
    # тесты не падали, если опрос задачи заглянул на итерацию больше ожидаемого
    def _respond(self, key) -> FakeResponse:
        queue = self._responses.get(key)
        if not queue:
            raise AssertionError(f"неожиданный вызов {key[0]} {key[1]}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return self._respond(("POST", path))

    def get(self, path):
        self.calls.append(("GET", path, None))
        return self._respond(("GET", path))

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        return self._respond(("DELETE", path))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_client(monkeypatch):
    """Подменяет httpx.Client(...) внутри OpenWebUIBackend.complete заглушкой."""

    def install(responses: dict) -> FakeHttpClient:
        client = FakeHttpClient(responses)
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
        return client

    return install


# вход: список вызовов FakeHttpClient.calls.
# выход: тело (json) первого POST /chat/completions среди них.
def _completions_body(calls):
    return next(c[2] for c in calls if c[:2] == ("POST", "/chat/completions"))


def test_complete_runs_path_a_and_deletes_the_chat(monkeypatch, fake_client):
    """Полный цикл: старт -> опрос задачи -> чтение ответа -> удаление чата."""
    monkeypatch.setattr(llm_backend, "LLM_TOOL_IDS", [])
    monkeypatch.setattr(llm_backend.time, "sleep", lambda seconds: None)

    client = fake_client({
        ("POST", "/chat/completions"): FakeResponse(200, {"chat_id": "chat-1"}),
        ("GET", "/tasks/chat/chat-1"): [
            FakeResponse(200, {"task_ids": ["t1"]}),
            FakeResponse(200, {"task_ids": []}),
        ],
        ("DELETE", "/v1/chats/chat-1"): FakeResponse(200, {}),
    })

    # id будущего ответа генерируется внутри _start и заранее неизвестен —
    # ответ /v1/chats/{id} подставляется отдельно, уже зная реальный id
    def fake_get(path):
        if path == "/v1/chats/chat-1":
            message_id = _completions_body(client.calls)["id"]
            client.calls.append(("GET", path, None))
            return FakeResponse(200, {
                "chat": {"history": {"messages": {message_id: {
                    "role": "assistant", "content": "ответ модели", "done": True,
                }}}},
            })
        return client._respond(("GET", path))

    monkeypatch.setattr(client, "get", fake_get)

    answer = OpenWebUIBackend().complete([HumanMessage(content="вопрос")])

    assert answer == "ответ модели"
    assert ("DELETE", "/v1/chats/chat-1") in [c[:2] for c in client.calls]
    assert client.closed

    post_call = next(c for c in client.calls if c[:2] == ("POST", "/chat/completions"))
    body = post_call[2]
    assert body["parent_id"] is None
    assert "chat_id" not in body
    assert body["user_message"]["content"] == "вопрос"
    assert "tool_ids" not in body


def test_complete_sends_configured_tool_ids(monkeypatch, fake_client):
    monkeypatch.setattr(llm_backend, "LLM_TOOL_IDS", ["debug_dump", "search"])
    monkeypatch.setattr(llm_backend.time, "sleep", lambda seconds: None)

    client = fake_client({
        ("POST", "/chat/completions"): FakeResponse(200, {"chat_id": "chat-1"}),
        ("GET", "/tasks/chat/chat-1"): FakeResponse(200, {"task_ids": []}),
        ("DELETE", "/v1/chats/chat-1"): FakeResponse(200, {}),
    })

    def fake_get(path):
        if path == "/v1/chats/chat-1":
            message_id = _completions_body(client.calls)["id"]
            return FakeResponse(200, {
                "chat": {"history": {"messages": {message_id: {"role": "assistant", "content": "ок"}}}},
            })
        return client._respond(("GET", path))

    monkeypatch.setattr(client, "get", fake_get)

    OpenWebUIBackend().complete([HumanMessage(content="вопрос")])

    assert _completions_body(client.calls)["tool_ids"] == ["debug_dump", "search"]


def test_complete_sends_image_parts_as_content_list(monkeypatch, fake_client):
    """Картинка уходит частью image_url в messages и ссылкой в files реплики чата."""
    monkeypatch.setattr(llm_backend, "LLM_TOOL_IDS", [])
    monkeypatch.setattr(llm_backend.time, "sleep", lambda seconds: None)

    client = fake_client({
        ("POST", "/chat/completions"): FakeResponse(200, {"chat_id": "chat-1"}),
        ("GET", "/tasks/chat/chat-1"): FakeResponse(200, {"task_ids": []}),
        ("DELETE", "/v1/chats/chat-1"): FakeResponse(200, {}),
    })

    def fake_get(path):
        if path == "/v1/chats/chat-1":
            message_id = _completions_body(client.calls)["id"]
            return FakeResponse(200, {
                "chat": {"history": {"messages": {message_id: {"role": "assistant", "content": "ок"}}}},
            })
        return client._respond(("GET", path))

    monkeypatch.setattr(client, "get", fake_get)

    parts = [
        {"type": "text", "text": "что на картинке?"},
        {"type": "image_url", "image_url": {"url": "img-1"}},
    ]
    OpenWebUIBackend().complete([HumanMessage(content=parts)])

    body = _completions_body(client.calls)
    assert body["messages"][-1]["content"] == parts
    assert body["user_message"]["content"] == "что на картинке?"
    assert body["user_message"]["files"] == [{"type": "image", "id": "img-1", "url": "img-1"}]
    assert "files" not in body


def test_complete_keeps_the_chat_when_delete_is_disabled(monkeypatch, fake_client):
    monkeypatch.setattr(llm_backend, "LLM_DELETE_CHAT", False)
    monkeypatch.setattr(llm_backend.time, "sleep", lambda seconds: None)

    client = fake_client({
        ("POST", "/chat/completions"): FakeResponse(200, {"chat_id": "chat-1"}),
        ("GET", "/tasks/chat/chat-1"): FakeResponse(200, {"task_ids": []}),
    })

    def fake_get(path):
        message_id = _completions_body(client.calls)["id"]
        return FakeResponse(200, {
            "chat": {"history": {"messages": {message_id: {"role": "assistant", "content": "ок"}}}},
        })

    monkeypatch.setattr(client, "get", fake_get)

    OpenWebUIBackend().complete([HumanMessage(content="вопрос")])

    assert ("DELETE", "/v1/chats/chat-1") not in [c[:2] for c in client.calls]


def test_complete_deletes_the_chat_even_when_the_task_times_out(monkeypatch, fake_client):
    """Отказ на любом шаге всё равно должен подчистить созданный чат."""
    monkeypatch.setattr(llm_backend, "LLM_TIMEOUT_SEC", 0)
    monkeypatch.setattr(llm_backend.time, "sleep", lambda seconds: None)

    client = fake_client({
        ("POST", "/chat/completions"): FakeResponse(200, {"chat_id": "chat-1"}),
        ("GET", "/tasks/chat/chat-1"): FakeResponse(200, {"task_ids": ["still-running"]}),
        ("DELETE", "/v1/chats/chat-1"): FakeResponse(200, {}),
    })

    with pytest.raises(LLMError, match="не ответила"):
        OpenWebUIBackend().complete([HumanMessage(content="вопрос")])

    assert ("DELETE", "/v1/chats/chat-1") in [c[:2] for c in client.calls]
    assert client.closed


def test_complete_raises_when_the_model_reports_an_error(monkeypatch, fake_client):
    monkeypatch.setattr(llm_backend.time, "sleep", lambda seconds: None)

    client = fake_client({
        ("POST", "/chat/completions"): FakeResponse(200, {"chat_id": "chat-1"}),
        ("GET", "/tasks/chat/chat-1"): FakeResponse(200, {"task_ids": []}),
        ("DELETE", "/v1/chats/chat-1"): FakeResponse(200, {}),
    })

    def fake_get(path):
        if path == "/v1/chats/chat-1":
            message_id = _completions_body(client.calls)["id"]
            return FakeResponse(200, {
                "chat": {"history": {"messages": {message_id: {
                    "role": "assistant", "content": "", "error": {"content": "модель упала"},
                }}}},
            })
        return client._respond(("GET", path))

    monkeypatch.setattr(client, "get", fake_get)

    with pytest.raises(LLMError, match="модель упала"):
        OpenWebUIBackend().complete([HumanMessage(content="вопрос")])
