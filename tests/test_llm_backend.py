"""Обращение к Open WebUI: нормализация адреса и разбор ответа модели.

Предмет нормализации — суффикс `/api`: генерация в Open WebUI идёт по
`/api/chat/completions`, а адрес в `.env` пишет человек. Ошибка выглядела бы
как 404 в глубине клиента, поэтому проверяется отдельно.

Сеть не нужна: проверяются чистые функции и обработка ответа, сам клиент
(`ChatOpenAI`) не создаётся.
"""

import pytest

from src import llm
from src.llm_backend import webui_url


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

    Если за Open WebUI развернута рассуждающая модель, а разбор рассуждений
    не настроен, теги приходят прямо в content.
    """
    fake_backend("<think>надо подумать</think>\nОтвет по существу.")

    assert llm.generate([], "вопрос") == "Ответ по существу."


def test_generate_rejects_empty_answer(fake_backend):
    """Пустой ответ — ошибка, а не письмо в пустоту.

    Пайплайн переведёт письмо в error, и его можно вернуть командой retry;
    молча отправленное пустое письмо выглядело бы как поломка модели.
    """
    fake_backend("<think>рассуждал, пока не кончился лимит</think>")

    with pytest.raises(RuntimeError, match="пустой ответ"):
        llm.generate([], "вопрос")


def test_generate_sends_no_system_prompt(fake_backend):
    """Системный промпт задан на модели sofi-mail в Open WebUI, не здесь.

    Свой SystemMessage в запросе встал бы рядом с промптом модели и тихо
    переопределил бы часть правил — поэтому уезжают только реплики переписки.
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
