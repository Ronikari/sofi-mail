# свёртка переписки: заменяет реплики сводкой и двигает границу контекста вперёд — письма
# до неё не попадают в запрос ни из базы, ни из цитаты нового письма.

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src import llm, storage
from src.config import (
    MAIL_DISPLAY_NAME,
    SESSION_MAX_CHARS,
    SUMMARY_COMPRESSION_RATIO,
    SUMMARY_ENABLED,
)
from src.email_parser import REPLY_MARKER
from src.storage import SUMMARY_ROLE

log = logging.getLogger(__name__)

# причины свёртки, попадают в колонку summaries.reason
REASON_LIMIT = "limit"
REASON_REQUEST = "request"

# нижняя граница объёма сводки: доля SUMMARY_COMPRESSION_RATIO от короткой переписки
# даёт несколько десятков знаков, в которые не помещается ни один вопрос с ответом.
MIN_TARGET_CHARS = 800

# распознавание просьбы словами снято: свёртка необратима, а формы «подведи итог» встречаются
# и в письме по делу — цена ложного срабатывания выше цены команды, повторённой вручную.
_COMMAND = re.compile(r"^\s*/summary\b", re.IGNORECASE)

# требования пронумерованы — модель держит короткий список надёжнее абзаца. пункт про
# авторов обязателен: без него диалог превращается в изложение от неизвестного лица.
_INSTRUCTION = """Сверни переписку ниже в одну сводку. Она заменит собой эти письма в контексте диалога.

Требования к сводке:
1. Указывай автора каждой мысли: «{peer}» — то, что написал пользователь, «{me}» — то, что ответила модель.
2. Сохрани суть: заданные вопросы, полученные ответы, решения, числа, названия, договорённости и открытые вопросы.
3. Время отправки писем не указывай.
4. Не отвечай на вопросы из переписки и не добавляй ничего от себя.
5. Уложись примерно в {target} символов.

В ответе — только текст сводки, без вступления и заголовка.

Тема переписки: {title}

--- переписка ---
{transcript}
--- конец переписки ---"""

# подпись строки в стенограмме: кто кому написал. форма «А → Б» задана прямо,
# потому что то же требование предъявляется к сводке
_FROM_USER = "{peer} → {me}:"
_FROM_MODEL = "{me} → {peer}:"
_FROM_SUMMARY = "Сводка более ранней переписки:"


# признак один — команда /summary первой строкой письма; ничто другое свёртку не запускает.
def is_summary_request(text: str) -> bool:
    """Определяет, просит ли письмо свернуть переписку в сводку."""
    if not text or not SUMMARY_ENABLED:
        return False

    return bool(_COMMAND.match(text))


# история читается заново, не берётся у вызывающего: с урезанным MAX_HISTORY_MESSAGES-окном
# свёртка потеряла бы старые реплики, не покрыв их сводкой.
def full_context(session_id: int) -> List[Dict[str, Any]]:
    """Читает контекст сессии целиком — то, что подлежит свёртке."""
    return storage.get_history(session_id)


# считается только текст реплик: служебные поля в запрос не уходят.
def context_chars(history: Sequence) -> int:
    """Считает объём контекста сессии в символах."""
    return sum(len(row["body"]) for row in history)


# текущий запрос входит в расчёт, но в свёртку не попадает: свёртка покрывает реплики,
# предшествующие ему, а ответ на него собирается уже по сводке.
def over_limit(history: Sequence, prompt: str) -> bool:
    """Сообщает, дошла ли сессия до предела, за которым нужна свёртка."""
    if not SUMMARY_ENABLED or SESSION_MAX_CHARS <= 0:
        return False

    # свернуть можно то, что состоит больше чем из одной строки: сессия
    # из единственной сводки после повторной свёртки короче не станет
    if len(history) < 2:
        return False

    return context_chars(history) + len(prompt) > SESSION_MAX_CHARS


# нижняя граница MIN_TARGET_CHARS защищает короткую переписку, верхняя (половина предела
# сессии) держит запас на новые письма после свёртки.
def target_chars(source_chars: int) -> int:
    """Считает целевую длину сводки по коэффициенту сжатия."""
    ceiling = max(MIN_TARGET_CHARS, SESSION_MAX_CHARS // 2)
    return max(MIN_TARGET_CHARS, min(int(source_chars * SUMMARY_COMPRESSION_RATIO), ceiling))


# подпись обязательна: переписка уходит одним текстом, без неё вопросы пользователя
# неотличимы от ответов модели.
def transcript(history: Sequence, peer_email: str) -> str:
    """Собирает переписку в текст с указанием автора каждой реплики."""
    peer = f"Пользователь ({peer_email})" if peer_email else "Пользователь"
    parts = []

    for row in history:
        body = row["body"].strip()

        if row["role"] == SUMMARY_ROLE:
            label = _FROM_SUMMARY
        elif row["role"] == "assistant":
            label = _FROM_MODEL.format(me=MAIL_DISPLAY_NAME, peer=peer)

            # метка снимается только у ассистента: автора и так называет подпись строки,
            # а у пользователя та же строка означала бы его собственную цитату.
            if body.startswith(REPLY_MARKER):
                body = body[len(REPLY_MARKER):].lstrip()
        else:
            label = _FROM_USER.format(me=MAIL_DISPLAY_NAME, peer=peer)

        parts.append(f"{label}\n{body}")

    # пустая строка между репликами: без неё модель склеивает соседние письма
    return "\n\n".join(parts)


# история в запрос не передаётся, переписка уходит текстом внутри промпта: передача
# историей поставила бы модель в позицию участника диалога, а не пересказчика.
def summarize(history: Sequence, title: str, peer_email: str) -> str:
    """Просит модель свернуть переписку сессии в сводку."""
    source_chars = context_chars(history)

    prompt = _INSTRUCTION.format(
        peer=f"Пользователь ({peer_email})" if peer_email else "Пользователь",
        me=MAIL_DISPLAY_NAME,
        target=target_chars(source_chars),
        title=title,
        transcript=transcript(history, peer_email),
    )

    text = llm.generate([], prompt).strip()

    # текст принимается даже длиннее оригинала: отказ оставил бы сессию за пределом
    # и запускал бы суммаризацию заново на каждом письме.
    if len(text) >= source_chars:
        log.warning(
            "сводка (%d символов) не короче свёрнутой переписки (%d символов)",
            len(text), source_chars,
        )

    return text


# нужна вызывающему коду до перечитывания базы: ответ на текущее письмо собирается
# по свёрнутому контексту сразу, без второго запроса к базе.
def context_row(body: str, covers_upto: int, created_at: str = "") -> Dict[str, Any]:
    """Приводит сводку к форме строки контекста сессии."""
    return {
        "id": covers_upto,
        "role": SUMMARY_ROLE,
        "body": body,
        "body_raw": None,
        "created_at": created_at or storage.now(),
    }


# вызывается под замком сессии — между чтением истории и записью сводки не должна
# попасть чужая реплика. граница берётся из последней строки контекста.
def fold(session_id: int, reason: str, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Сворачивает контекст сессии в сводку и отдаёт контекст из неё одной."""
    # контекст читается здесь, а не берётся аргументом: вызывающий код читает историю
    # с ограничением MAX_HISTORY_MESSAGES, пришла бы только её часть.
    history = full_context(session_id)

    if not history:
        return []

    session = storage.get_session(session_id)
    title = session["title"] if session else ""
    peer_email = session["peer_email"] if session else ""

    covers_upto = int(history[-1]["id"])
    covers_chars = context_chars(history)

    body = summarize(history, title, peer_email)

    log.info(
        "сессия %s свёрнута (%s): %d строк, %d символов -> %d символов",
        session_id, reason, len(history), covers_chars, len(body),
    )

    if not dry_run:
        storage.add_summary(session_id, body, covers_upto, covers_chars, reason)

    return [context_row(body, covers_upto)]


# признак поднимается только при реальной замене реплик сводкой — по нему pipeline.py
# предупреждает пользователя. Сбой суммаризации не режет контекст, запрос уходит полным.
def fit_session(
    session_id: Optional[int], history: Sequence, prompt: str, dry_run: bool = False
) -> Tuple[List[Dict[str, Any]], bool]:
    """Сворачивает переписку, если сессия доросла до предела объёма."""
    rows = list(history)

    if not session_id or not over_limit(rows, prompt):
        return rows, False

    log.info(
        "сессия %s достигла предела %d символов: %d в истории, %d в письме с вложениями",
        session_id, SESSION_MAX_CHARS, context_chars(rows), len(prompt),
    )

    try:
        folded = fold(session_id, REASON_LIMIT, dry_run)
    except Exception as exc:
        log.error("свёртка сессии %s не удалась, запрос уходит целиком: %s", session_id, exc)
        return rows, False

    # пустой результат fold означает сессию, в которой сворачивать нечего:
    # контекст остаётся прежним, и предупреждать пользователя не о чем
    return (folded, True) if folded else (rows, False)
