# работа с файлами в Open WebUI: загрузка, ожидание готовности, удаление,
# уборка по сроку хранения.
# порядок: текст документа -> POST /api/v1/files/ -> опрос статуса обработки ->
# ссылка на файл для тела запроса к модели -> DELETE по истечении срока.
# вход: имя файла и извлечённый текст из attachments.py; списки просроченных
# идентификаторов из storage.list_expired_files.
# выход: идентификатор файла в Open WebUI, ссылка вида {"type": "file", "id": ...}
# для llm_backend.complete, счётчики удалённых файлов.
# ATTACHMENT_PROCESS_TIMEOUT_SEC, ATTACHMENT_RETENTION_DAYS, LLM_BASE_URL
# и LLM_TIMEOUT_SEC импортируются из config.py; ssl_context, auth_headers
# и webui_url — из llm_backend.py; отметки об удалении пишет storage.py.
# вызывается из pipeline.py и cli.py (команды check, files, purge, purge-files, forget).
#
# эндпоинты Open WebUI 0.8.12:
#   POST   /api/v1/files/                     загрузка, ответ содержит id файла
#   GET    /api/v1/files/{id}/process/status  готовность файла к использованию
#   DELETE /api/v1/files/{id}                 удаление
#
# файл остаётся на стороне Open WebUI после отправки ответа, поэтому у него
# действует собственный срок хранения ATTACHMENT_RETENTION_DAYS.
# идентификатор файла хранится в таблице session_files: следующее письмо треда
# ссылается на тот же документ, повторная загрузка создала бы второй файл
# с тем же содержимым в общем хранилище

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

# пауза между опросами готовности файла в секундах. документ на несколько
# страниц готов к первому опросу, поэтому опрос начинается без задержки,
# а пауза удваивается только при ожидании
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


# вход: suffix — хвост пути после /files/, пустая строка даёт адрес коллекции.
# выход: полный адрес файлового эндпоинта.
# ветка /v1 здесь принадлежит внутреннему rest Open WebUI; генерация живёт
# по адресу /api/chat/completions, и запрет суффикса /v1 в LLM_BASE_URL
# к этому адресу отношения не имеет
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
    raise FileError(f"{what}: Open WebUI ответил {response.status_code}{hint} — {response.text[:200]}")


# вход: filename — имя файла в хранилище; text — извлечённый текст документа.
# выход: идентификатор файла в Open WebUI, строка.
# побочный эффект: http-запрос POST, файл появляется в хранилище сервисной
# учётной записи и остаётся там до вызова delete
def upload(filename: str, text: str) -> str:
    """Загружает текст документа файлом и возвращает его идентификатор."""
    # текст кодируется в utf-8 один раз: те же байты уходят в запрос и в лог
    data = text.encode("utf-8")

    with _client() as client:
        response = client.post(
            _files_url(),
            headers=auth_headers(),
            # multipart-поле file принимает тройку «имя, байты, mime-тип»
            files={"file": (filename, data, "text/plain; charset=utf-8")},
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


# вход: file_id из upload; timeout — предел ожидания в секундах.
# выход отсутствует; поднимает FileError при отказе обработки и по таймауту.
# предусловие: файл загружен вызовом upload.
# запрос к модели, отправленный до готовности файла, при context=full получает
# пустой документ, в режиме поиска получает пустую выдачу
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

            # статус из набора _FAILED означает отказ обработки на сервере
            if status in _FAILED:
                raise FileError(f"Open WebUI не смог обработать файл (статус {status})")

            # проверка дедлайна идёт после разбора статуса: последнее полученное
            # значение попадает в текст ошибки
            if time.monotonic() >= deadline:
                raise FileError(f"файл не готов за {timeout} с (последний статус {status or '—'})")

            time.sleep(delay)

            # пауза удваивается до предела _POLL_MAX_SEC секунд
            delay = min(delay * 2, _POLL_MAX_SEC)


# вход: идентификатор файла в Open WebUI.
# выход: True при удалении файла и при его отсутствии, False при отказе сервера.
# побочный эффект: http-запрос DELETE
def delete(file_id: str) -> bool:
    """Удаляет файл в Open WebUI."""
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


# вход: file_id из upload; full_context — признак подачи документа целиком.
# выход: словарь для поля files тела запроса, его передаёт llm_backend.complete
def reference(file_id: str, full_context: bool) -> Dict[str, Any]:
    """Собирает ссылку на загруженный файл для тела запроса к модели."""
    link: Dict[str, Any] = {"type": "file", "id": file_id}

    # значение full кладёт в контекст весь текст документа; для большого
    # документа такой текст вытесняет историю переписки и сам вопрос.
    # решение о режиме принимает attachments.ParsedDocument.full_context
    if full_context:
        link["context"] = "full"

    return link


# --- Срок хранения ----------------------------------------------------------
# Единственное место, где сетевой вызов встречается с базой: удалить файл нужно
# на той стороне, а знание о том, какие файлы просрочены, — на этой.


# вход: идентификаторы файлов в Open WebUI.
# выход: число удалённых файлов.
# побочные эффекты: http-запросы DELETE и запись deleted_at в таблицу session_files
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


# вход: days — срок хранения в сутках, значение 0 и меньше отключает уборку.
# выход: пара (удалено, осталось); остаток образуют файлы, которые сервер
# отказался удалить.
# побочные эффекты те же, что у forget
def purge_expired(days: int = ATTACHMENT_RETENTION_DAYS) -> Tuple[int, int]:
    """Удаляет из Open WebUI файлы старше указанного срока."""
    from src import storage

    # значение 0 и меньше означает бессрочное хранение: файлы накапливаются
    # в Open WebUI, уборка не выполняется
    if days <= 0:
        return (0, 0)

    # список просроченных строк собирает storage.list_expired_files по created_at
    expired = [row["file_id"] for row in storage.list_expired_files(days)]
    if not expired:
        return (0, 0)

    removed = forget(expired)
    if removed:
        log.info("удалено файлов в Open WebUI по сроку хранения (%d дней): %d", days, removed)

    return (removed, len(expired) - removed)


# выход: строка с адресом файлового API и числом файлов сервисной учётной записи.
# поднимает FileError при недоступности API.
# побочный эффект: http-запрос GET
def describe() -> str:
    """Отдаёт строку о доступности файлового API для команды check."""
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
    # значение -1 отмечает неразобранный счётчик; доступность API при этом
    # уже подтверждена вызовом _raise_for выше, и check остаётся успешным
    except Exception:
        count = -1

    where = _files_url()
    return f"{where}, файлов у сервисной учётки: {count if count >= 0 else '—'}"
