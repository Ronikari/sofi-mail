# Тесты сборки контекста запроса к модели (llm.build_messages) — состав и порядок сообщений
# langchain; сеть не используется, MAX_CONTEXT_CHARS подменяется через monkeypatch.

from src import llm


def test_history_fits_under_the_limit(monkeypatch):
    """История сессии входит в запрос, пока укладывается в лимит."""
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 1000)

    history = [
        {"role": "user", "body": "первый вопрос"},
        {"role": "assistant", "body": "первый ответ"},
    ]
    messages = llm.build_messages(history, "второй вопрос")

    # порядок хронологический, текущее письмо занимает последнюю позицию
    assert [m.content for m in messages] == [
        "первый вопрос", "первый ответ", "второй вопрос",
    ]


def test_oversized_prompt_keeps_history(monkeypatch):
    """Запрос длиннее порога историю сессии не отбрасывает."""
    # Текст письма (с блоком описаний вложений) может сам перерасти порог, но отбор реплик истории
    # здесь не происходит — это дело warn_over_budget, иначе потеря контекста была бы незаметна.
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 100)

    history = [{"role": "user", "body": "старый вопрос"}]
    prompt = "я" * 150
    messages = llm.build_messages(history, prompt)

    assert [m.content for m in messages] == ["старый вопрос", prompt]


def test_reply_longer_than_budget_stays_in_context(monkeypatch):
    """Раздутая реплика в истории остаётся в запросе вместе с остальными."""
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 200)

    history = [
        {"role": "user", "body": "короткая старая реплика"},
        {"role": "assistant", "body": "я" * 500},
    ]
    messages = llm.build_messages(history, "вопрос")

    assert [m.content for m in messages] == ["короткая старая реплика", "я" * 500, "вопрос"]


def test_build_messages_takes_history_from_warn_over_budget(monkeypatch):
    """build_messages берёт историю у warn_over_budget — единственной точки отбора реплик."""
    # Подмена показывает, что другой точки отбора реплик в модуле нет — реальная свёртка живёт
    # в summarizer.fit_session и вызывается раньше, в pipeline._answer.
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 1000)
    monkeypatch.setattr(
        llm, "warn_over_budget", lambda history, budget: [{"role": "user", "body": "сводка"}]
    )

    history = [{"role": "user", "body": "первый вопрос"}, {"role": "assistant", "body": "первый ответ"}]
    messages = llm.build_messages(history, "второй вопрос")

    assert [m.content for m in messages] == ["сводка", "второй вопрос"]


def test_warn_over_budget_returns_history_unchanged():
    """warn_over_budget не режет историю ни при каком бюджете — это диагностика, не защита."""
    history = [{"role": "user", "body": "я" * 5000}, {"role": "assistant", "body": "ответ"}]

    assert llm.warn_over_budget(history, budget=10) == history


def test_images_become_image_url_parts_of_the_last_message(monkeypatch):
    """Картинка встаёт в текущее письмо частью image_url, в поле files её нет."""
    sent = {}

    class Backend:
        def complete(self, messages, files):
            sent["messages"], sent["files"] = messages, list(files)
            return "ответ"

    monkeypatch.setattr(llm, "get_backend", lambda: Backend())

    files = [{"type": "file", "id": "doc-1"}, {"type": "image", "id": "img-1"}]
    llm.generate([{"role": "user", "body": "старый вопрос"}], "что на картинке?", files)

    assert sent["files"] == [{"type": "file", "id": "doc-1"}]
    assert sent["messages"][0].content == "старый вопрос"
    assert sent["messages"][-1].content == [
        {"type": "text", "text": "что на картинке?"},
        {"type": "image_url", "image_url": {"url": "img-1"}},
    ]
