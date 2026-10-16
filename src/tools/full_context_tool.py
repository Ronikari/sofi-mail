"""
title: Full Context Attachments
author: sofi-mail
version: 0.1.0
license: MIT
description: >
  Отдаёт модели содержимое приложенных к запросу документов целиком, минуя
  фокусированный поиск (RAG). Инструмент вызывается, когда к запросу приложены
  файлы и ответ требует документа в целом: пересказ, сверка разделов, поиск
  того, чего в документе нет.
requirements:
"""

# инструмент Open WebUI: подаёт приложенные документы целиком вместо фокусированного поиска —
# решение остаётся за моделью, раз Full Context Mode интерфейса на API-запросы не доходит.

# __files__, __user__, __event_emitter__ — служебные аргументы, подставляет их Open WebUI.

# Valves.max_chars — общий предел объёма (лишнее называется в хвосте ответа поимённо).
# Valves.allowed_api_users — список email/id, пустой список — без ограничений.

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Tools:
    # настройки инструмента, доступные администратору в интерфейсе Open WebUI
    class Valves(BaseModel):
        max_chars: int = Field(
            default=200_000,
            description=(
                "Предел суммарного объёма документов в знаках. Ориентир — окно "
                "модели в токенах, умноженное на 3 для русского текста, "
                "с запасом на историю и ответ."
            ),
        )
        max_file_chars: int = Field(
            default=100_000,
            description=(
                "Предел на один документ в знаках. Держит пакет от того, чтобы "
                "первый же файл занял весь бюджет и вытеснил остальные."
            ),
        )
        include_names: bool = Field(
            default=True,
            description="Подписывать каждый документ его именем в тексте ответа.",
        )
        allowed_api_users: List[str] = Field(
            default_factory=list,
            description=(
                "Список email или id пользователей, которым разрешён вызов "
                "инструмента. Пустой список снимает ограничение — доступ "
                "открыт всем, как раньше."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

        # цитаты интерфейса отключены: инструмент отдаёт текст самих
        # документов, и Open WebUI показал бы его вторым списком под ответом
        self.citation = False

    # __files__ подставляет Open WebUI — файлы текущего запроса с уже извлечённым текстом.
    async def full_context(
        self,
        query: str,
        has_attachments: bool = True,
        __files__: Optional[List[Dict[str, Any]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __event_emitter__=None,
    ) -> str:
        """
        Получить содержимое приложенных к запросу документов целиком, без
        фокусированного поиска. Вызывать, когда к запросу приложены файлы
        и ответ требует документа в целом: пересказ, перечисление всех
        упоминаний, сверка разделов, проверка отсутствия чего-либо в документе.

        :param query: вопрос пользователя, ради которого нужен полный документ.
        :param has_attachments: True, если к запросу приложены файлы.
        :return: текст документов с подписями либо объяснение, почему его нет.
        """
        # состав __files__/__user__ разнится по сборкам Open WebUI — без лога промах
        # («документов нет») пришлось бы разбирать вслепую; текст документов в лог не попадает.
        log.info("full_context: __user__=%r", __user__)
        log.info("full_context: __files__=%s", _debug_files(__files__))

        # allowed_api_users пуст — ограничения нет, доступ как раньше;
        # непустой — вызов разрешён только пользователям из списка
        if self.valves.allowed_api_users and not _is_allowed_user(
            __user__, self.valves.allowed_api_users
        ):
            await _status(__event_emitter__, "Доступ к инструменту закрыт", done=True)
            return (
                "Этому пользователю не разрешён доступ к инструменту полного "
                "чтения документов — сообщите об этом в ответе."
            )

        # модель сообщила, что вложений нет: поиск и чтение файлов пропускаются
        if not has_attachments:
            return (
                "К запросу не приложено документов — отвечайте по тексту "
                "самого запроса."
            )

        files = _documents(__files__ or [])

        # список пуст при запросе без вложений и при вложениях, которые
        # Open WebUI ещё не обработал
        if not files:
            return (
                "К запросу не приложено документов, доступных для чтения. "
                "Если файл был приложен, его обработка ещё не закончилась — "
                "скажите об этом пользователю и предложите повторить вопрос."
            )

        await _status(__event_emitter__, f"Читаю документы целиком: {len(files)}")

        parts: List[str] = []
        dropped: List[str] = []
        used = 0

        for name, text in files:
            # предел на файл применяется до общего: иначе первый документ
            # пакета занял бы весь бюджет
            body = text[: self.valves.max_file_chars]

            # остаток общего бюджета; отрицательным он не бывает
            budget = self.valves.max_chars - used

            # документ не помещается даже началом: он называется в хвосте
            # ответа, чтение продолжается — следующий файл бывает короче
            if budget <= 0:
                dropped.append(name)
                continue

            # документ помещается частью: обрезанный текст отмечается прямо
            # в теле, чтобы модель не выдала оборванный раздел за полный
            if len(body) > budget:
                body = body[:budget] + "\n[…документ обрезан по пределу инструмента…]"
                dropped.append(name)

            used += len(body)
            parts.append(f"### {name}\n{body}" if self.valves.include_names else body)

        answer = (
            f"Содержимое приложенных документов ({len(parts)} из {len(files)}), "
            f"вопрос пользователя: {query}\n\n" + "\n\n".join(parts)
        )

        # хвост про обрезанные документы: без него ответ по половине пакета
        # выглядит как ответ по всему пакету
        if dropped:
            answer += (
                "\n\nВ предел инструмента поместились не все документы. "
                f"Не поместились целиком: {', '.join(dropped)}. "
                "Скажите об этом пользователю в ответе."
            )

        await _status(__event_emitter__, "Документы прочитаны", done=True)
        return answer


# сверяются id и email — состав __user__ разнится между вызовом из чата и по API.
def _is_allowed_user(user: Optional[Dict[str, Any]], allowed: List[str]) -> bool:
    """Проверяет пользователя запроса против списка допущенных."""
    if not isinstance(user, dict):
        return False

    candidates = {str(user.get("id") or ""), str(user.get("email") or "")}
    candidates.discard("")

    return bool(candidates & set(allowed))


# поля читаются теми же путями, что и _documents — по логу видно, на каком ключе промахнулся разбор.
# content обрезан до 200 знаков: лог инструмента шире круга допущенных к документу.
def _debug_files(files: Optional[List[Dict[str, Any]]]) -> str:
    """Собирает состав файлов запроса строкой для лога."""
    if not files:
        return repr(files)

    preview = []
    for item in files:
        # нестандартный элемент попадает в лог как есть: разбирать в нём нечего
        if not isinstance(item, dict):
            preview.append(repr(item))
            continue

        source = item.get("file") if isinstance(item.get("file"), dict) else item
        data = source.get("data") if isinstance(source.get("data"), dict) else {}
        text = data.get("content") or source.get("content") or ""
        preview.append({
            "keys": list(item.keys()),
            "filename": source.get("filename") or source.get("name"),
            "id": source.get("id") or item.get("id"),
            "content_len": len(text) if isinstance(text, str) else None,
            "content_preview": text[:200] if isinstance(text, str) else text,
        })
    return repr(preview)


# состав полей разнится по сборкам: плоский словарь либо обёртка с ключом file, текст —
# в data.content либо content верхнего уровня; при пустом тексте читается из хранилища по id.
def _documents(files: List[Dict[str, Any]]) -> List[tuple]:
    """Достаёт имена и извлечённый текст из файлов запроса."""
    found = []

    for item in files:
        if not isinstance(item, dict):
            continue

        # обёртка {"type": "file", "file": {...}} приходит от чата,
        # плоский словарь — от вызова по API
        source = item.get("file") if isinstance(item.get("file"), dict) else item

        data = source.get("data") if isinstance(source.get("data"), dict) else {}
        text = data.get("content") or source.get("content") or ""

        meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
        name = source.get("filename") or source.get("name") or meta.get("name") or "документ"

        # при включённом поиске Open WebUI кладёт в files только метаданные — текст читается
        # из хранилища отдельно, иначе инструмент молча отвечал бы «документов нет».
        if not isinstance(text, str) or not text.strip():
            text = _content_from_store(source.get("id") or item.get("id"))

        # файл без текста и в хранилище: обработка ещё идёт либо документ пуст
        if not text.strip():
            continue

        found.append((str(name), text))

    return found


# импорт внутренних моделей Open WebUI — по месту вызова: имена модулей разнятся по сборкам,
# промах не должен ломать инструмент целиком.
def _content_from_store(file_id: Optional[str]) -> str:
    """Читает извлечённый текст файла из хранилища Open WebUI."""
    if not file_id:
        return ""

    try:
        from open_webui.models.files import Files

        record = Files.get_file_by_id(str(file_id))

    # ветка другой сборки Open WebUI: модуля нет либо изменилось имя метода
    except Exception:
        return ""

    if record is None:
        return ""

    # поле data у записи файла — словарь с ключом content
    data = getattr(record, "data", None) or {}
    content = data.get("content") if isinstance(data, dict) else ""
    return content if isinstance(content, str) else ""


# emitter бывает None при вызове вне чата (запросам sofi-mail статус не виден) — не ошибка.
async def _status(emitter, text: str, done: bool = False) -> None:
    """Пишет статус инструмента в интерфейс Open WebUI."""
    if emitter is None:
        return

    await emitter(
        {"type": "status", "data": {"description": text, "done": done}}
    )
