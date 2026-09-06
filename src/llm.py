# сборка контекста для запроса к модели и разбор её ответа.
# порядок: история сессии и текст письма -> обрезка под лимит символов ->
# список сообщений langchain -> вызов шлюза -> удаление блоков рассуждений ->
# проверка ответа на пустоту.
# вход: строки таблицы messages (role, body) и текст входящего письма; ссылки
# на загруженные документы приходят из owui_files.reference.
# выход: текст ответа модели; при пустом ответе поднимается RuntimeError.
# MAX_CONTEXT_CHARS и LLM_MODEL импортируются из config.py, сам запрос
# выполняет llm_backend.py.
# вызывается из pipeline.py и cli.py (команды ask и check).
#
# системный промпт в этом модуле отсутствует: промпт, параметры генерации,
# базы знаний и фильтры заданы на модели sofi-mail в рабочем пространстве
# Open WebUI. промпт, переданный в запросе, перекрывает часть правил модели

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

# вход: history — строки таблицы messages (role, body), упорядоченные от старых
# к новым; prompt — текст входящего письма.
# выход: список сообщений langchain, суммарная длина укладывается
# в MAX_CONTEXT_CHARS; текущее письмо занимает последнюю позицию.
# побочные эффекты отсутствуют, аргумент history остаётся неизменным
def build_messages(history: Sequence, prompt: str) -> List[BaseMessage]:
    """Собирает контекст запроса из истории сессии и текущего письма."""
    # бюджет для истории: лимит символов за вычетом длины текущего письма.
    # текущее письмо входит в запрос при любом размере истории, без него
    # в запросе отсутствует вопрос пользователя.
    # значение MAX_CONTEXT_CHARS задано в config.py и измеряется в символах;
    # окно модели принадлежит sofi-mail в Open WebUI и клиенту недоступно,
    # эта обрезка удерживает запрос в пределах, которые сервер принимает
    budget = MAX_CONTEXT_CHARS - len(prompt)

    # копия в список: аргумент допускает тип курсора бд с одним проходом
    history = list(history)

    # kept накапливает отобранные сообщения, dropped считает отброшенные реплики
    kept: List[BaseMessage] = []
    dropped = 0

    # reversed даёт перебор от свежих реплик к старым: при исчерпании бюджета
    # за пределами запроса остаются самые старые реплики
    for row in reversed(history):
        # длина тела реплики в символах, MAX_CONTEXT_CHARS задан в тех же единицах
        cost = len(row["body"])

        # реплика длиннее остатка бюджета пропускается, перебор идёт дальше.
        # break на этой строке отбросил бы всю оставшуюся историю из-за одной
        # длинной реплики: письмо, в тело которого попал весь тред целиком
        if cost > budget:
            dropped += 1
            continue

        # реплика принята, её длина списывается с остатка бюджета
        budget -= cost

        # значение role из таблицы messages переводится в класс сообщения
        # langchain: assistant даёт AIMessage, остальные значения HumanMessage
        kept.append(AIMessage(content=row["body"]) if row["role"] == "assistant" else HumanMessage(content=row["body"]))

    # факт обрезки пишется в лог: расхождение числа реплик в бд и в запросе
    # объясняет ответы модели с потерей части истории
    if dropped:
        log.info("история обрезана: в запрос поместилось %d реплик из %d", len(kept), len(history))

    # kept заполнялся от свежих реплик к старым, reversed восстанавливает
    # хронологию; текущее письмо занимает последнюю позицию
    return [*reversed(kept), HumanMessage(content=prompt)]


# вход: history и prompt в том же виде, что у build_messages; files — ссылки
# на документы, загруженные в Open WebUI функцией owui_files.reference.
# выход: текст ответа модели без блоков рассуждений.
# текст документов через этот модуль не проходит, он остаётся в Open WebUI.
# побочный эффект: http-запрос к Open WebUI через llm_backend.py
def generate(
    history: Sequence, prompt: str, files: Sequence[Dict[str, Any]] = ()
) -> str:
    """Возвращает ответ модели на письмо с учётом истории сессии и вложений."""
    messages = build_messages(history, prompt)

    log.debug(
        "запрос к %s: %d сообщений в контексте, файлов %d", LLM_MODEL, len(messages), len(files)
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


# выход: строка с адресом шлюза и именем модели.
# поднимает RuntimeError при недоступности сервера, отказе по ключу
# и отсутствии LLM_MODEL в списке моделей
def check_llm() -> str:
    """Отдаёт строку о доступности Open WebUI для команды check."""
    return get_backend().describe()
