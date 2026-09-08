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
    # реплики сессии при этом остаются в запросе: их отбор — дело
    # warn_over_budget, и молчаливая потеря контекста здесь была бы
    # неотличима от новой сессии
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
    # подмена показывает, что другой точки отбора реплик в модуле нет.
    # реальная свёртка живёт в summarizer.fit_session и вызывается раньше,
    # в pipeline._answer — сюда история приходит уже свёрнутой при надобности
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
