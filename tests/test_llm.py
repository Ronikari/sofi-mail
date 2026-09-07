# тесты сборки контекста запроса к модели.
# порядок: build_messages вызывается с историей и текстом письма -> утверждение
# сверяет состав и порядок сообщений langchain.
# вход: MAX_CONTEXT_CHARS подменяется через monkeypatch.
# выход: результат pytest.
# проверяется llm.py; сеть здесь не используется, шлюз не вызывается.
# запуск: pytest tests/test_llm.py

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
    # текст письма приходит сюда из pipeline вместе с блоком описаний
    # документов и перерастает порог при нескольких больших вложениях.
    # реплики сессии при этом остаются в запросе: их отбор — дело fit_context,
    # и молчаливая потеря контекста здесь была бы неотличима от новой сессии
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


def test_fit_context_is_the_summarisation_hook(monkeypatch):
    """build_messages берёт историю у fit_context — точки будущей суммаризации."""
    # подмена показывает, что другой точки отбора реплик в модуле нет:
    # суммаризация встанет сюда и заменит старые реплики сводкой
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 1000)
    monkeypatch.setattr(llm, "fit_context", lambda history, budget: [{"role": "user", "body": "сводка"}])

    history = [{"role": "user", "body": "первый вопрос"}, {"role": "assistant", "body": "первый ответ"}]
    messages = llm.build_messages(history, "второй вопрос")

    assert [m.content for m in messages] == ["сводка", "второй вопрос"]


def test_fit_context_returns_history_unchanged():
    """Пока суммаризации нет, fit_context историю не режет ни при каком бюджете."""
    history = [{"role": "user", "body": "я" * 5000}, {"role": "assistant", "body": "ответ"}]

    assert llm.fit_context(history, budget=10) == history
