"""Генерация ответа моделью sofi-mail.

Здесь всё, что не зависит от шлюза: сборка контекста под лимит запроса и разбор
ответа. Сам вызов уходит в `src/llm_backend.py`.

Системного промпта в проекте нет намеренно. Он, как и параметры генерации,
знания и фильтры, задан на модели sofi-mail в рабочем пространстве Open WebUI:
одно место правки вместо двух, и правка не требует ни выкладки образа, ни
доступа к серверу. Демон отправляет только реплики переписки — свой промпт
в запросе встал бы рядом с промптом модели и тихо переопределил бы часть правил.
"""

import logging
import re
from typing import Any, Dict, List, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from src.config import LLM_MODEL, MAX_CONTEXT_CHARS
from src.llm_backend import get_backend

log = logging.getLogger(__name__)

# Отсечение рассуждений. Open WebUI отдаёт их отдельным полем, если модель
# развёрнута с разбором рассуждений; если нет — теги приходят прямо в content,
# и рассуждения уехали бы пользователю письмом.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Сделать суммаризацию, если достигнут лимит
def build_messages(history: Sequence, prompt: str) -> List[BaseMessage]:
    """История сессии + текущее письмо.

    История обрезается с начала (самые старые реплики) под `MAX_CONTEXT_CHARS`:
    текущее письмо не выбрасывается никогда, иначе модель потеряет сам вопрос.
    Лимит здесь клиентский и грубый — точное окно принадлежит модели sofi-mail
    в Open WebUI, и оттуда оно не видно; задача этой обрезки лишь в том, чтобы
    сервер не отверг запрос целиком из-за разросшейся истории.

    Реплика, не влезающая в остаток бюджета, пропускается, а перебор
    продолжается. Раньше на ней перебор обрывался — и одна раздутая реплика
    (например, письмо, в тело которого уехал весь тред целиком) выбрасывала
    из контекста всю остальную историю сессии, оставляя модели один последний
    вопрос.
    """
    budget = MAX_CONTEXT_CHARS - len(prompt)

    history = list(history)
    kept: List[BaseMessage] = []
    dropped = 0
    for row in reversed(history):
        cost = len(row["body"])
        if cost > budget:
            dropped += 1
            continue
        budget -= cost
        kept.append(AIMessage(content=row["body"]) if row["role"] == "assistant" else HumanMessage(content=row["body"]))

    if dropped:
        log.info("история обрезана: в запрос поместилось %d реплик из %d", len(kept), len(history))

    return [*reversed(kept), HumanMessage(content=prompt)]


def generate(
    history: Sequence, prompt: str, files: Sequence[Dict[str, Any]] = ()
) -> str:
    """Ответ модели на письмо с учётом истории сессии и вложений.

    `files` — ссылки на документы, уже загруженные в Open WebUI
    (`owui_files.reference`). Сами документы через этот модуль не проходят:
    их текст живёт на той стороне, здесь остаётся только ссылка.
    """
    messages = build_messages(history, prompt)
    log.debug(
        "запрос к %s: %d сообщений в контексте, файлов %d", LLM_MODEL, len(messages), len(files)
    )
    text = _THINK_BLOCK.sub("", get_backend().complete(messages, files)).strip()
    if not text:
        raise RuntimeError(
            "модель вернула пустой ответ (вероятно, лимит ответа на модели "
            "sofi-mail в Open WebUI израсходован на рассуждения)"
        )
    return text


def check_llm() -> str:
    """Доступность Open WebUI и наличие модели — для команды `check`."""
    return get_backend().describe()
