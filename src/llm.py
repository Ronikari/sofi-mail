"""Генерация ответа локальной моделью.

Здесь всё, что не зависит от сервера инференса: системный промпт, сборка
контекста под окно модели и разбор ответа. Сам вызов уходит в `src/llm_backend.py`.
"""

import logging
import re
from typing import List, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from src.config import LLM_MAX_TOKENS, LLM_MODEL, LLM_NUM_CTX, SYSTEM_PROMPT_FILE
from src.llm_backend import get_backend

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """Ты — ассистент, который общается с пользователем по электронной почте.

Правила:
1. Отвечай на языке письма пользователя.
2. Ответ читают в почтовом клиенте как обычный текст: не используй markdown-разметку \
(**жирный**, # заголовки, таблицы) — вместо неё обычные абзацы и списки с дефисом.
3. Пиши по делу и структурно; длину подбирай под вопрос, не растекайся.
4. Не повторяй вопрос пользователя и не начинай с приветствия в каждом письме — \
переписка уже идёт.
5. Не добавляй подпись и прощание: подпись подставляется автоматически.
6. Учитывай историю переписки в этой сессии — это один непрерывный диалог."""

# Отсечение рассуждений. Qwen2.5-72B-Instruct не рассуждает, и для неё это
# холостой проход, который ничего не стоит. Но если на сервере развернут
# рассуждающую модель и запустят vLLM без ключа --reasoning-parser, теги придут
# прямо в content — и рассуждения уехали бы пользователю письмом.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def load_system_prompt() -> str:
    """Системный промпт из файла, чтобы правки не требовали правки кода."""
    if SYSTEM_PROMPT_FILE.exists():
        text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    log.warning("файл системного промпта %s не найден, использую встроенный", SYSTEM_PROMPT_FILE)
    return DEFAULT_SYSTEM_PROMPT


def estimate_tokens(text: str) -> int:
    """Грубая оценка длины в токенах.

    Точный подсчёт требовал бы токенизатора модели; для решения «влезает или
    нет» достаточно ~3 символов на токен (для русского это ближе к правде,
    чем привычные 4 для английского).
    """
    return len(text) // 3 + 1


def build_messages(history: Sequence, prompt: str) -> List[BaseMessage]:
    """Системный промпт + история сессии + текущее письмо.

    История обрезается с начала (самые старые реплики) под окно модели:
    system-промпт и текущее письмо не выбрасываются никогда, иначе модель
    потеряет либо инструкции, либо сам вопрос.
    """
    system_prompt = load_system_prompt()
    budget = LLM_NUM_CTX - LLM_MAX_TOKENS - 512  # запас на служебные токены
    budget -= estimate_tokens(system_prompt) + estimate_tokens(prompt)

    kept: List[BaseMessage] = []
    for row in reversed(list(history)):
        cost = estimate_tokens(row["body"])
        if cost > budget:
            log.info("история обрезана: в окно поместилось %d реплик из %d", len(kept), len(history))
            break
        budget -= cost
        kept.append(AIMessage(content=row["body"]) if row["role"] == "assistant" else HumanMessage(content=row["body"]))

    return [SystemMessage(content=system_prompt), *reversed(kept), HumanMessage(content=prompt)]


def generate(history: Sequence, prompt: str) -> str:
    """Ответ модели на письмо с учётом истории сессии."""
    messages = build_messages(history, prompt)
    log.debug("запрос к %s: %d сообщений в контексте", LLM_MODEL, len(messages))
    text = _THINK_BLOCK.sub("", get_backend().complete(messages)).strip()
    if not text:
        raise RuntimeError(
            "модель вернула пустой ответ (вероятно, LLM_MAX_TOKENS израсходован на рассуждения)"
        )
    return text


def check_llm() -> str:
    """Доступность сервера инференса и наличие модели — для команды `check`."""
    return get_backend().describe()
