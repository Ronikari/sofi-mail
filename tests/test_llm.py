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


def test_oversized_prompt_drops_history_without_error(monkeypatch):
    """Запрос длиннее лимита оставляет историю за пределами контекста."""
    # текст письма приходит сюда из pipeline вместе с блоком описаний
    # документов и перерастает лимит при нескольких больших вложениях.
    # отрицательный бюджет пропускал бы каждую реплику через сравнение
    # cost > budget и давал бы тот же пустой контекст молча
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 100)

    history = [{"role": "user", "body": "старый вопрос"}]
    prompt = "я" * 150
    messages = llm.build_messages(history, prompt)

    assert len(messages) == 1
    assert messages[0].content == prompt


def test_reply_longer_than_budget_is_skipped_without_breaking_the_loop(monkeypatch):
    """Реплика длиннее остатка бюджета пропускается, перебор продолжается."""
    # одно раздутое письмо в истории не должно вытеснять из запроса остальные
    # реплики сессии
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 200)

    history = [
        {"role": "user", "body": "короткая старая реплика"},
        {"role": "assistant", "body": "я" * 500},
    ]
    messages = llm.build_messages(history, "вопрос")

    assert [m.content for m in messages] == ["короткая старая реплика", "вопрос"]
