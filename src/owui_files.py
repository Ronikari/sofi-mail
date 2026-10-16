# работа с файлами в Open WebUI: загрузка, ожидание готовности, удаление, уборка по сроку —
# файл грузится как есть, разбор делает сервер; file_id кешируется в session_files.

import json
import logging
import time
from typing import Any, Dict, List, Sequence, Tuple

from src.attachments import DEFAULT_CONTENT_TYPE
from src.config import (
    ATTACHMENT_DELETE_ENABLED,
    ATTACHMENT_PROCESS_TIMEOUT_SEC,
    ATTACHMENT_RETENTION_DAYS,
    LLM_BASE_URL,
    LLM_TIMEOUT_SEC,
)
from src.llm_backend import auth_headers, ssl_context, webui_url

log = logging.getLogger(__name__)

# документ на несколько страниц готов уже к первому опросу, поэтому начинаем без
# задержки, пауза удваивается только при ожидании.
_POLL_START_SEC = 0.5
_POLL_MAX_SEC = 5.0

# Статусы, которые Open WebUI возвращает для загруженного файла. Неизвестное
# значение считаем промежуточным и опрашиваем дальше — до общего таймаута
_DONE = {"completed", "complete", "processed", "success", "done"}
_FAILED = {"failed", "error", "cancelled"}


# отказ на любом шаге работы с файлом; текст исключения попадает в примечание
# к письму пользователя, собираемое в pipeline.py
class FileError(RuntimeError):
    pass


# отказ по самому файлу (формат, повреждение, разбор) — отделён от FileError:
# повтор не поможет, pipeline.py останавливает письмо и не идёт к модели с половиной документов.
class FileRejected(FileError):
    pass


# /v1 здесь — внутренний rest Open WebUI, не тот /v1, что запрещён в LLM_BASE_URL
# (генерация живёт на /api/chat/completions).
def _files_url(suffix: str = "") -> str:
    """Собирает адрес файлового API из базового адреса Open WebUI."""
    return f"{webui_url(LLM_BASE_URL)}/v1/files/{suffix}"


# выход: клиент httpx с таймаутом LLM_TIMEOUT_SEC и тем же доверием
# к внутреннему УЦ, что и запросы к модели
def _client():
    """Создаёт http-клиент для запросов к файловому API."""
    import httpx

    context = ssl_context()

    # значение True включает проверку сертификата по умолчанию, когда
    # корневой сертификат внутреннего УЦ не задан
    return httpx.Client(verify=context if context else True, timeout=LLM_TIMEOUT_SEC)


# вход: response — ответ httpx; what — описание операции для текста ошибки.
# поднимает FileError при коде 400 и выше, при меньшем коде возвращает None
def _raise_for(response, what: str) -> None:
    """Переводит http-ошибку файлового API в FileError с текстом причины."""
    # коды ниже 400 обозначают успех, обработка продолжается у вызывающего
    if response.status_code < 400:
        return

    # коды 401 и 403 указывают на ключ сервисной учётной записи
    hint = " (проверьте LLM_API_KEY)" if response.status_code in (401, 403) else ""

    # тело ответа обрезается до 200 символов: страницы ошибок сервера приходят
    # разметкой html на несколько килобайт
    message = f"{what}: Open WebUI ответил {response.status_code}{hint} — {response.text[:200]}"

    # 4xx кроме 401/403 — отказ по самому файлу, повтор даст тот же код, поэтому
    # отдельный класс; 401/403 — это ключ учётки, а не файл, остаются FileError.
    if 400 <= response.status_code < 500 and response.status_code not in (401, 403):
        raise FileRejected(message)

    raise FileError(message)


# вход: filename — имя файла в хранилище; data — байты файла из письма;
# content_type — mime-тип для поля multipart; process=False — без разбора и индексации.
# выход: идентификатор файла в Open WebUI, строка.
# побочный эффект: http-запрос POST, файл появляется в хранилище сервисной
# учётной записи и остаётся там до вызова delete
def upload(
    filename: str, data: bytes, content_type: str = DEFAULT_CONTENT_TYPE, *, process: bool = True
) -> str:
    """Загружает файл письма как есть и возвращает его идентификатор."""
    with _client() as client:
        response = client.post(
            _files_url(),
            params=None if process else {"process": "false"},
            headers=auth_headers(),
            # парсер Open WebUI выбирается по расширению имени, поэтому имя
            # сохраняет расширение исходного файла.
            files={"file": (filename, data, content_type)},
        )
        _raise_for(response, f"загрузка {filename}")
        payload = _json(response, f"загрузка {filename}")

    # сборки Open WebUI называют поле идентификатора id либо file_id
    file_id = payload.get("id") or payload.get("file_id")

    # ответ без идентификатора: файл загружен, сослаться на него в запросе
    # к модели нечем
    if not file_id:
        raise FileError(f"загрузка {filename}: в ответе нет id файла ({str(payload)[:200]})")

    log.info("файл %s загружен в Open WebUI как %s (%d байт)", filename, file_id, len(data))
    return str(file_id)


# вход: response — ответ httpx; what — описание операции для текста ошибки.
# выход: тело ответа словарём
def _json(response, what: str) -> Dict[str, Any]:
    """Разбирает тело ответа как json-объект."""
    try:
        return dict(response.json())
    # ветка ответа, не разбираемого как json-объект: страница ошибки прокси,
    # пустое тело, json-массив верхнего уровня
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise FileError(f"{what}: ответ не разобрать как JSON") from exc


# запрос к модели, отправленный до готовности файла, получает пустую выдачу
# поиска — индекс документа к этому моменту ещё не построен.
def wait_processed(file_id: str, timeout: int = ATTACHMENT_PROCESS_TIMEOUT_SEC) -> None:
    """Опрашивает статус файла, пока Open WebUI не закончит его обработку."""
    # монотонные часы не зависят от перевода системного времени
    deadline = time.monotonic() + timeout
    delay = _POLL_START_SEC

    with _client() as client:
        while True:
            response = client.get(_files_url(f"{file_id}/process/status"), headers=auth_headers())

            # код 404 приходит от сборок Open WebUI старше 0.8, эндпоинт статуса
            # в них отсутствует; обработка продолжается без ожидания готовности
            if response.status_code == 404:
                log.warning(
                    "Open WebUI не отдаёт статус обработки файла %s (404): "
                    "продолжаю без ожидания готовности", file_id
                )
                return

            _raise_for(response, f"статус файла {file_id}")
            status = str(_json(response, f"статус файла {file_id}").get("status", "")).lower()

            # статус из набора _DONE завершает ожидание успехом
            if status in _DONE:
                return

            # статус из набора _FAILED означает отказ обработки на сервере:
            # файл загружен, но разобрать его не удалось — повтор бесполезен
            if status in _FAILED:
                raise FileRejected(f"Open WebUI не смог обработать файл (статус {status})")

            # проверка дедлайна идёт после разбора статуса: последнее полученное
            # значение попадает в текст ошибки
            if time.monotonic() >= deadline:
                raise FileError(f"файл не готов за {timeout} с (последний статус {status or '—'})")

            time.sleep(delay)

            # пауза удваивается до предела _POLL_MAX_SEC секунд
            delay = min(delay * 2, _POLL_MAX_SEC)


# ATTACHMENT_DELETE_ENABLED=false — политика хранения (файл остаётся при purge/forget).
# Файл без строки в session_files (брак, orphans) под неё не подпадает — код шлёт force=True.
def delete(file_id: str, *, force: bool = False) -> bool:
    """Удаляет файл в Open WebUI."""
    if not ATTACHMENT_DELETE_ENABLED and not force:
        log.debug("удаление файлов отключено (ATTACHMENT_DELETE_ENABLED=false): %s", file_id)
        return False

    try:
        with _client() as client:
            response = client.delete(_files_url(file_id), headers=auth_headers())

    # ветка сетевого сбоя: файл остаётся в хранилище, отметка об удалении
    # в бд не ставится, и файл попадает в следующую уборку
    except Exception as exc:
        log.warning("файл %s не удалён (%s), повтор при следующей уборке", file_id, exc)
        return False

    # код 404 означает, что файла в хранилище уже нет
    if response.status_code == 404:
        return True

    # прочие коды 4xx и 5xx: сервер отказал, файл остаётся на месте
    if response.status_code >= 400:
        log.warning("файл %s не удалён: %s %s", file_id, response.status_code, response.text[:120])
        return False

    return True


# вход: file_id из upload; filename — имя файла; image=True для изображения.
# выход: {"type": "image", "id"} для изображения — llm.generate переносит его
# в текст вопроса частью image_url; {"type": "file", "id", "name", "url"} для документа.
# context не ставится — режим подачи выбирает Open WebUI сам. name/url обязательны:
# add_file_context в middleware Open WebUI отсеивает файлы без url раньше, чем дойдёт до имени.
def reference(file_id: str, filename: str = "", *, image: bool = False) -> Dict[str, Any]:
    """Собирает ссылку на загруженный файл для тела запроса к модели."""
    if image:
        return {"type": "image", "id": file_id}
    ref: Dict[str, Any] = {"type": "file", "id": file_id}
    if filename:
        ref["name"] = filename
        ref["url"] = _files_url(f"{file_id}/content")
    return ref


# --- Срок хранения --- единственное место, где сетевой вызов встречается с базой:
# удалить файл нужно на той стороне, знание о просрочке — на этой.


# удаляет на стороне Open WebUI и пишет deleted_at в session_files.
def forget(file_ids: Sequence[str]) -> int:
    """Удаляет перечисленные файлы в Open WebUI и отмечает удаление в базе."""
    # импорт по месту вызова разрывает цикл: storage.py и этот модуль
    # используются независимо друг от друга
    from src import storage

    removed = 0
    for file_id in file_ids:
        # отметка в бд ставится по факту удаления на стороне Open WebUI:
        # при отказе сервера строка остаётся живой и попадает в следующую уборку
        if delete(file_id):
            storage.mark_file_deleted(file_id)
            removed += 1

    return removed


# остаток пары (удалено, осталось) — файлы, которые сервер отказался удалить.
def purge_expired(days: int = ATTACHMENT_RETENTION_DAYS) -> Tuple[int, int]:
    """Удаляет из Open WebUI файлы старше указанного срока."""
    from src import storage

    # значение 0 и меньше означает бессрочное хранение: файлы накапливаются
    # в Open WebUI, уборка не выполняется
    if days <= 0:
        return (0, 0)

    # выключенное удаление обрывает уборку до запросов к серверу: каждый файл
    # иначе дал бы отказ delete и запись в лог
    if not ATTACHMENT_DELETE_ENABLED:
        log.info("уборка файлов пропущена: ATTACHMENT_DELETE_ENABLED=false")
        return (0, 0)

    # список просроченных строк собирает storage.list_expired_files по created_at
    expired = [row["file_id"] for row in storage.list_expired_files(days)]
    if not expired:
        return (0, 0)

    removed = forget(expired)
    if removed:
        log.info("удалено файлов в Open WebUI по сроку хранения (%d дней): %d", days, removed)

    return (removed, len(expired) - removed)


# список нужен команде reconcile — сверяется с session_files и показывает файлы,
# оставшиеся в хранилище без строки в базе.
def list_remote() -> List[Tuple[str, str]]:
    """Читает список файлов сервисной учётной записи в Open WebUI."""
    with _client() as client:
        response = client.get(_files_url(), headers=auth_headers())
    _raise_for(response, "файловый API")

    payload = _json_any(response, "список файлов")

    # ответ приходит либо постранично в поле items, либо списком верхнего уровня
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise FileError(f"список файлов: неожиданная структура ответа ({str(payload)[:200]})")

    found = []
    for item in items:
        if not isinstance(item, dict):
            continue

        # имя файла лежит в filename либо во вложенном meta.name, состав полей
        # разнится по сборкам Open WebUI
        file_id = item.get("id") or item.get("file_id")
        if not file_id:
            continue

        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        found.append((str(file_id), str(item.get("filename") or meta.get("name") or "")))

    return found


# в отличие от _json список верхнего уровня не приводится к словарю.
def _json_any(response, what: str) -> Any:
    """Разбирает тело ответа как json произвольной структуры."""
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise FileError(f"{what}: ответ не разобрать как JSON") from exc


# поднимает FileError при недоступности файлового API.
def describe() -> str:
    """Отдаёт строку о доступности файлового API для команды check."""
    with _client() as client:
        response = client.get(_files_url(), headers=auth_headers())
    _raise_for(response, "файловый API")

    # ответ постраничный {"items": [...], "total": N} — len() от словаря раньше
    # давал число ключей (всегда 2), а не число файлов.
    try:
        payload = response.json()
        if isinstance(payload, dict):
            count = payload.get("total", len(payload.get("items", [])))
        else:
            count = len(payload)
    # значение -1 отмечает неразобранный счётчик; доступность API при этом
    # уже подтверждена вызовом _raise_for выше, и check остаётся успешным
    except Exception:
        count = -1

    where = _files_url()
    return f"{where}, файлов у сервисной учётки: {count if count >= 0 else '—'}"
