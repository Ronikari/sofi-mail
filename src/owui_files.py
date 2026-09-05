"""Файлы в Open WebUI: загрузка, готовность, удаление, срок хранения.

Отдельно от `src/llm_backend.py`, потому что это другой контракт: там один
запрос-ответ без состояния, здесь — объекты, которые остаются на сервере после
того, как ответ отправлен, и за которыми надо убирать.

Три эндпоинта, ради которых модуль существует (Open WebUI 0.8.12):

  POST   /api/v1/files/                  загрузка, возвращает id файла
  GET    /api/v1/files/{id}/process/status  готов ли файл к использованию
  DELETE /api/v1/files/{id}              удаление

Чат в Open WebUI при этом не создаётся (`/api/v1/chats/new`): история переписки
живёт в SQLite, где у неё есть срок хранения, удаление по требованию
и маскирование в логах. Файл — единственное, что от переписки остаётся
на чужой стороне, поэтому у него есть свой срок (`ATTACHMENT_RETENTION_DAYS`)
и своя уборка, а не «когда-нибудь почистим руками».

`file_id` живёт в `session_files` не ради экономии: follow-up письмо того же
треда должно спрашивать про тот же документ, а повторная загрузка дала бы
второй файл с тем же содержимым и удвоила бы то, что лежит в общем хранилище.
"""

import json
import logging
import time
from typing import Any, Dict, Sequence, Tuple

from src.config import (
    ATTACHMENT_PROCESS_TIMEOUT_SEC,
    ATTACHMENT_RETENTION_DAYS,
    LLM_BASE_URL,
    LLM_TIMEOUT_SEC,
)
from src.llm_backend import auth_headers, ssl_context, webui_url

log = logging.getLogger(__name__)

# Пауза между опросами готовности файла. Мелкий документ обычно готов сразу,
# поэтому первый опрос идёт без задержки, а пауза растёт только при ожидании
_POLL_START_SEC = 0.5
_POLL_MAX_SEC = 5.0

# Статусы, которые Open WebUI возвращает для загруженного файла. Неизвестное
# значение считаем промежуточным и опрашиваем дальше — до общего таймаута
_DONE = {"completed", "complete", "processed", "success", "done"}
_FAILED = {"failed", "error", "cancelled"}


class FileError(RuntimeError):
    """Файл не удалось довести до пригодного к использованию состояния."""


def _files_url(suffix: str = "") -> str:
    """Адрес файлового API: тот же хост, что и генерация, но ветка /v1.

    Генерация живёт в `/api/chat/completions`, файлы — в `/api/v1/files/`:
    в Open WebUI это разные ветки одного API, и `/v1` здесь не имеет отношения
    к суффиксу vLLM, который `validate` запрещает в `LLM_BASE_URL`.
    """
    return f"{webui_url(LLM_BASE_URL)}/v1/files/{suffix}"


def _client():
    """HTTP-клиент с тем же доверием к внутреннему УЦ, что и запросы к модели."""
    import httpx

    context = ssl_context()
    return httpx.Client(verify=context if context else True, timeout=LLM_TIMEOUT_SEC)


def _raise_for(response, what: str) -> None:
    """HTTP-ошибку — в текст, по которому понятно, что чинить."""
    if response.status_code < 400:
        return
    hint = " (проверьте LLM_API_KEY)" if response.status_code in (401, 403) else ""
    raise FileError(f"{what}: Open WebUI ответил {response.status_code}{hint} — {response.text[:200]}")


def upload(filename: str, text: str) -> str:
    """Загрузить текст документа файлом. Возвращает id файла в Open WebUI."""
    data = text.encode("utf-8")
    with _client() as client:
        response = client.post(
            _files_url(),
            headers=auth_headers(),
            files={"file": (filename, data, "text/plain; charset=utf-8")},
        )
        _raise_for(response, f"загрузка {filename}")
        payload = _json(response, f"загрузка {filename}")

    file_id = payload.get("id") or payload.get("file_id")
    if not file_id:
        raise FileError(f"загрузка {filename}: в ответе нет id файла ({str(payload)[:200]})")
    log.info("файл %s загружен в Open WebUI как %s (%d байт)", filename, file_id, len(data))
    return str(file_id)


def _json(response, what: str) -> Dict[str, Any]:
    try:
        return dict(response.json())
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise FileError(f"{what}: ответ не разобрать как JSON") from exc


def wait_processed(file_id: str, timeout: int = ATTACHMENT_PROCESS_TIMEOUT_SEC) -> None:
    """Дождаться, пока Open WebUI разберёт и проиндексирует файл.

    Без ожидания запрос к модели уходит раньше, чем файл готов: при
    `context=full` он приедет пустым, а в режиме поиска по нему не найдётся
    ничего — и то и другое выглядит как «модель не увидела документ».

    Эндпоинта статуса может не оказаться (сборка старше 0.8): тогда 404
    считается «сервер не умеет отвечать на этот вопрос», и мы идём дальше,
    записав предупреждение, — это лучше, чем отказать пользователю в ответе.
    """
    deadline = time.monotonic() + timeout
    delay = _POLL_START_SEC
    with _client() as client:
        while True:
            response = client.get(_files_url(f"{file_id}/process/status"), headers=auth_headers())
            if response.status_code == 404:
                log.warning(
                    "Open WebUI не отдаёт статус обработки файла %s (404): "
                    "продолжаю без ожидания готовности", file_id
                )
                return
            _raise_for(response, f"статус файла {file_id}")
            status = str(_json(response, f"статус файла {file_id}").get("status", "")).lower()
            if status in _DONE:
                return
            if status in _FAILED:
                raise FileError(f"Open WebUI не смог обработать файл (статус {status})")
            if time.monotonic() >= deadline:
                raise FileError(f"файл не готов за {timeout} с (последний статус {status or '—'})")
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SEC)


def delete(file_id: str) -> bool:
    """Удалить файл. True — удалён или его уже нет; False — сервер отказал."""
    try:
        with _client() as client:
            response = client.delete(_files_url(file_id), headers=auth_headers())
    except Exception as exc:  # сеть, TLS, таймаут — уборка повторится завтра
        log.warning("файл %s не удалён (%s), повтор при следующей уборке", file_id, exc)
        return False

    if response.status_code == 404:
        return True  # удалён раньше или руками — цель достигнута
    if response.status_code >= 400:
        log.warning("файл %s не удалён: %s %s", file_id, response.status_code, response.text[:120])
        return False
    return True


def reference(file_id: str, full_context: bool) -> Dict[str, Any]:
    """Ссылка на файл для тела запроса к модели.

    `context=full` просит Open WebUI положить в контекст весь текст документа
    вместо релевантных фрагментов. Ставится только маленьким документам:
    у большого он вытеснил бы и историю переписки, и сам вопрос.
    """
    link: Dict[str, Any] = {"type": "file", "id": file_id}
    if full_context:
        link["context"] = "full"
    return link


# --- Срок хранения ----------------------------------------------------------
# Единственное место, где сетевой вызов встречается с базой: удалить файл нужно
# на той стороне, а знание о том, какие файлы просрочены, — на этой.


def forget(file_ids: Sequence[str]) -> int:
    """Удалить перечисленные файлы в Open WebUI и отметить это в базе."""
    from src import storage

    removed = 0
    for file_id in file_ids:
        if delete(file_id):
            storage.mark_file_deleted(file_id)
            removed += 1
    return removed


def purge_expired(days: int = ATTACHMENT_RETENTION_DAYS) -> Tuple[int, int]:
    """Удалить файлы старше `days` дней. Возвращает (удалено, осталось).

    days <= 0 — срок не задан, файлы копятся в Open WebUI бессрочно; тогда
    уборки нет вовсе, и это осознанный выбор администратора, а не умолчание.
    """
    from src import storage

    if days <= 0:
        return (0, 0)
    expired = [row["file_id"] for row in storage.list_expired_files(days)]
    if not expired:
        return (0, 0)
    removed = forget(expired)
    if removed:
        log.info("удалено файлов в Open WebUI по сроку хранения (%d дней): %d", days, removed)
    return (removed, len(expired) - removed)


def describe() -> str:
    """Строка для команды `check`: файловый API доступен и отвечает."""
    with _client() as client:
        response = client.get(_files_url(), headers=auth_headers())
    _raise_for(response, "файловый API")
    # Ответ постраничный: {"items": [...], "total": N}. Раньше здесь стоял
    # len() от разобранного JSON — от словаря он даёт число ключей, то есть
    # всегда 2, сколько бы файлов ни лежало. Список без обёртки на всякий
    # случай тоже принимается: считать его нечем, кроме len()
    try:
        payload = response.json()
        if isinstance(payload, dict):
            count = payload.get("total", len(payload.get("items", [])))
        else:
            count = len(payload)
    except Exception:
        count = -1
    where = _files_url()
    return f"{where}, файлов у сервисной учётки: {count if count >= 0 else '—'}"
