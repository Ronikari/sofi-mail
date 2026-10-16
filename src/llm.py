# сборка контекста для запроса к модели и разбор её ответа. Системный промпт здесь не
# задаётся — он на модели sofi-mail в Open WebUI, текст запроса лишь перекрывает часть правил.

import logging
import re
from typing import Any, Dict, List, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from src.config import LLM_MODEL, MAX_CONTEXT_CHARS
from src.llm_backend import get_backend
from src.storage import SUMMARY_ROLE

log = logging.getLogger(__name__)

# Отсечение рассуждений: если модель развёрнута без разбора reasoning, теги <think>
# приходят прямо в content, и рассуждения уехали бы пользователю письмом.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# сводка уходит репликой пользователя, не системным сообщением — оно перекрыло бы
# правила модели sofi-mail. вторая фраза учит модель предпочитать сводку цитате свёрнутых писем.
SUMMARY_PREFIX = (
    "Сводка предыдущей переписки этой сессии. Письма, вошедшие в сводку, "
    "в контекст больше не входят: если в новом письме процитированы более "
    "ранние сообщения, опирайся на сводку, а не на цитату.\n\n"
)


# роль assistant даёт AIMessage, роль сводки — реплику пользователя с шапкой,
# остальные значения — обычную реплику пользователя.
def _as_message(row) -> BaseMessage:
    """Переводит строку контекста сессии в сообщение langchain."""
    if row["role"] == "assistant":
        return AIMessage(content=row["body"])

    if row["role"] == SUMMARY_ROLE:
        return HumanMessage(content=SUMMARY_PREFIX + row["body"])

    return HumanMessage(content=row["body"])


# объём сессии держит свёртка в summarizer.py; эта функция лишь логирует превышение
# уже после того решения и историю не режет — молчаливая потеря реплик была бы неотличима от новой сессии.
def warn_over_budget(history: Sequence, budget: int) -> List:
    """Логирует превышение бюджета запроса, историю отдаёт без изменений."""
    # копия в список: аргумент допускает тип курсора бд с одним проходом
    rows = list(history)

    total = sum(len(row["body"]) for row in rows)

    # превышение бюджета пишется в лог: по этой записи видно, что свёртка
    # до сессии не дошла, и запрос ушёл на сервер во весь размер
    if total > budget:
        log.warning(
            "контекст сессии (%d строк, %d символов) выходит за бюджет %d символов; "
            "запрос уходит целиком",
            len(rows), total, budget,
        )

    return rows


# текущее письмо занимает последнюю позицию; аргумент history остаётся неизменным.
def build_messages(history: Sequence, prompt: str) -> List[BaseMessage]:
    """Собирает контекст запроса из истории сессии и текущего письма."""
    # текущее письмо входит в запрос при любом размере истории. max(0, ...) нужен,
    # потому что prompt приходит уже с блоком описаний вложений и может перерасти порог.
    budget = max(0, MAX_CONTEXT_CHARS - len(prompt))

    # одно письмо длиннее всего порога: истории бюджета не остаётся вовсе,
    # запись в лог называет причину заранее.
    if len(prompt) >= MAX_CONTEXT_CHARS:
        log.warning(
            "запрос длиной %d символов достиг порога %d: бюджета под историю не осталось",
            len(prompt), MAX_CONTEXT_CHARS,
        )

    rows = warn_over_budget(history, budget)

    # роль строки переводится в класс сообщения langchain
    kept: List[BaseMessage] = [_as_message(row) for row in rows]

    # текущее письмо занимает последнюю позицию
    return [*kept, HumanMessage(content=prompt)]


# вход: history и prompt в том же виде, что у build_messages; files — ссылки
# на документы и изображения, загруженные в Open WebUI функцией
# owui_files.reference.
# выход: текст ответа модели без блоков рассуждений.
# текст документов через этот модуль не проходит, он остаётся в Open WebUI.
# побочный эффект: http-запрос к Open WebUI через llm_backend.py
def generate(
    history: Sequence, prompt: str, files: Sequence[Dict[str, Any]] = ()
) -> str:
    """Возвращает ответ модели на письмо с учётом истории сессии и вложений."""
    messages = build_messages(history, prompt)

    # поле files Open WebUI отдаёт поиску по индексу, у картинки индекса нет.
    # id файла в url сервер заменяет байтами в base64 (convert_url_images_to_base64)
    images = [f["id"] for f in files if f.get("type") == "image"]
    files = [f for f in files if f.get("type") != "image"]
    if images:
        messages[-1] = HumanMessage(content=[
            {"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": file_id}} for file_id in images),
        ])

    log.debug(
        "запрос к %s: %d сообщений в контексте, файлов %d, изображений %d",
        LLM_MODEL, len(messages), len(files), len(images),
    )

    # блоки <think> удаляются до всех проверок: текст внутри них попал бы
    # в письмо пользователю
    text = _THINK_BLOCK.sub("", get_backend().complete(messages, files)).strip()

    # пустая строка после удаления рассуждений означает, что лимит ответа
    # израсходован моделью на рассуждения; письмо-пустышка не отправляется
    if not text:
        raise RuntimeError(
            "модель вернула пустой ответ (вероятно, лимит ответа на модели "
            "sofi-mail в Open WebUI израсходован на рассуждения)"
        )
    return text


# поднимает RuntimeError при недоступности сервера, отказе по ключу и отсутствии
# LLM_MODEL в списке моделей.
def check_llm() -> str:
    """Отдаёт строку о доступности Open WebUI для команды check."""
    return get_backend().describe()
