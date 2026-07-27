"""Генерация ответа локальной моделью через Ollama."""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import List, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from src.config import (
    LLM_MODEL,
    LLM_TIMEOUT_SEC,
    OLLAMA_BASE_URL,
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
    OLLAMA_TEMPERATURE,
    SYSTEM_PROMPT_FILE,
)

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

# у qwen3 рассуждения не отключаются: `think: false` лишь убирает теги, а сами
# рассуждения утекают в текст, `/no_think` игнорируется. Нативный API отдаёт их
# отдельным полем, поэтому content чистый — регулярка ниже только страховка.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def load_system_prompt() -> str:
    """Системный промпт из файла, чтобы правки не требовали правки кода."""
    if SYSTEM_PROMPT_FILE.exists():
        text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    log.warning("файл системного промпта %s не найден, использую встроенный", SYSTEM_PROMPT_FILE)
    return DEFAULT_SYSTEM_PROMPT


def get_llm() -> ChatOllama:
    """Клиент Ollama через нативный API (не /v1 — там нельзя задать num_ctx).

    keep_alive=-1: между письмами могут проходить часы, и без этого каждый ответ
    начинался бы с повторной загрузки модели в память.
    """
    return ChatOllama(
        base_url=OLLAMA_BASE_URL,
        model=LLM_MODEL,
        temperature=OLLAMA_TEMPERATURE,
        num_predict=OLLAMA_NUM_PREDICT,  # thinking + ответ, см. комментарий выше
        num_ctx=OLLAMA_NUM_CTX,
        keep_alive=-1,
        client_kwargs={"timeout": LLM_TIMEOUT_SEC},
    )


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
    budget = OLLAMA_NUM_CTX - OLLAMA_NUM_PREDICT - 512  # запас на служебные токены
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
    answer = get_llm().invoke(messages)
    text = _THINK_BLOCK.sub("", str(answer.content)).strip()
    if not text:
        raise RuntimeError("модель вернула пустой ответ (вероятно, num_predict израсходован на рассуждения)")
    return text


def check_ollama() -> str:
    """Доступность Ollama и наличие нужной модели — для команды `check`."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5) as response:
            tags = json.load(response)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama недоступна на {OLLAMA_BASE_URL}: {exc.reason}") from exc

    models = [m.get("name", "") for m in tags.get("models", [])]
    if LLM_MODEL not in models:
        raise RuntimeError(
            f"модель {LLM_MODEL} не найдена. Доступны: {', '.join(models) or 'нет моделей'}. "
            f"Скачать: ollama pull {LLM_MODEL}"
        )
    return f"{OLLAMA_BASE_URL}, модель {LLM_MODEL}"
