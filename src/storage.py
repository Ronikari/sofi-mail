# Хранилище переписки в sqlite: sessions — тред, messages — реплики, summaries — свёртка
# старых реплик, processed — журнал «один ответ на письмо», session_files — вложения в Open WebUI.

import logging
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from src.config import DB_PATH, RETENTION_DAYS

log = logging.getLogger(__name__)

# Права заданы явно (umask ослабляет их выборочно): иначе любой пользователь сервера
# прочитает переписку через sqlite3; режим каталога закрывает и WAL-файлы.
_DIR_MODE = 0o700
_FILE_MODE = 0o600

# Роль сводки в контексте сессии: в таблице messages такой роли нет, её подставляет get_history.
SUMMARY_ROLE = "summary"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    peer_email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_peer_title ON sessions(peer_email, title);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK(role IN ('user','assistant')),
    body        TEXT NOT NULL,
    body_raw    TEXT,
    message_id  TEXT UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    body         TEXT NOT NULL,
    -- messages.id последней реплики, вошедшей в сводку: реплики с меньшим
    -- и равным идентификатором в контекст сессии больше не попадают
    covers_upto  INTEGER NOT NULL,
    covers_chars INTEGER NOT NULL DEFAULT 0,
    -- причина свёртки: limit (достигнут SESSION_MAX_CHARS) либо request
    -- (пользователь попросил письмом)
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, id);

CREATE TABLE IF NOT EXISTS processed (
    message_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    detail       TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_status ON processed(status);

CREATE TABLE IF NOT EXISTS session_files (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_id      TEXT NOT NULL UNIQUE,
    filename     TEXT NOT NULL,
    -- вес файла в байтах: содержимое документа проект не хранит и не разбирает,
    -- вес — единственная его мера, известная на этой стороне
    bytes        INTEGER NOT NULL DEFAULT 0,
    message_id   TEXT,
    created_at   TEXT NOT NULL,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_files_session ON session_files(session_id, id);
CREATE INDEX IF NOT EXISTS idx_session_files_alive ON session_files(deleted_at, created_at);
"""


# ISO-8601 в utc сортируется лексикографически, поэтому сравнения дат в SQL работают на текстовых колонках.
def now() -> str:
    """Отдаёт текущее время в едином для базы текстовом формате."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Проверяется на каждом подключении: база из бэкапа, архива или старой версии демона
# может остаться доступной на чтение всем пользователям сервера.
def _restrict_permissions(db_path: Path) -> None:
    """Снимает у базы и её каталога права доступа для всех, кроме владельца."""
    # каталог обрабатывается перед файлом: mkdir(mode=...) в connect задаёт
    # права только новому каталогу, и umask их урезает
    for target, mode in ((db_path.parent, _DIR_MODE), (db_path, _FILE_MODE)):
        try:
            # отсутствующий файл базы появится после первого запроса sqlite
            if not target.exists():
                continue

            # S_IMODE оставляет от режима только биты прав доступа
            current = stat.S_IMODE(target.stat().st_mode)

            # условие срабатывает при наличии битов сверх целевого режима:
            # права сужаются, расширение прав здесь не выполняется
            if current & ~mode:
                os.chmod(target, mode)
                log.info("права на %s ужесточены: %o -> %o", target, current, mode)

        # на сетевом хранилище chmod запрещён; демон продолжает работу,
        # запись в лог отмечает неработающую защиту
        except OSError as exc:
            log.warning(
                "не удалось ограничить права на %s (%s): переписка может быть "
                "доступна другим пользователям сервера", target, exc
            )


# Открывает соединение с ужесточением прав; commit — при выходе из with без исключения, закрытие — всегда.
@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Открывает соединение с базой и закрывает его на выходе из блока with."""
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

    # timeout 10 секунд задаёт ожидание блокировки: письма пишут в базу
    # параллельные воркеры
    conn = sqlite3.connect(db_path, timeout=10)

    # только после connect: сам файл базы создаёт SQLite, и до этого момента
    # ужесточать права было бы не на чем — новая база осталась бы с 0644
    _restrict_permissions(db_path)

    # row_factory=Row открывает доступ к полям по имени, на этом построены
    # все читатели модуля
    conn.row_factory = sqlite3.Row

    # WAL допускает чтение параллельно с записью: команды sessions/history работают,
    # пока демон пишет в ту же базу.
    conn.execute("PRAGMA journal_mode=WAL")

    # foreign_keys=ON включает ON DELETE CASCADE: при значении по умолчанию
    # sqlite оставляет реплики и файлы удалённой сессии в базе
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        yield conn
        # commit выполняется при выходе из блока with без исключения;
        # исключение оставляет транзакцию откатанной закрытием соединения
        conn.commit()
    finally:
        conn.close()


# Колонки, добавленные после первого релиза: CREATE TABLE IF NOT EXISTS их не добавит существующим
# таблицам. Признак — наличие колонки, а не PRAGMA user_version: номер версий расходится у копий и бэкапов базы.
_ADDED_COLUMNS = (
    # Вес файла добавлен при переходе разбора документов на сторону Open WebUI; старым строкам
    # достаётся 0 («объём неизвестен»). Старые колонки pages/chars/full_context/outline из схемы убраны.
    ("session_files", "bytes", "INTEGER NOT NULL DEFAULT 0"),
)


# Вызывается на каждом init_db (каждый запуск демона и команда cli); на полной схеме ничего не меняет.
def _migrate(conn: sqlite3.Connection) -> None:
    """Добавляет в существующую базу колонки, появившиеся после её создания."""
    for table, column, ddl in _ADDED_COLUMNS:
        # PRAGMA table_info отдаёт описание колонок таблицы
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

        if not columns:  # страховка: таблицы нет — дополнять нечего
            continue

        # колонка отсутствует в существующей таблице: DEFAULT в ddl заполняет
        # её у всех имеющихся строк
        if column not in columns:
            log.info("миграция базы: %s.%s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Безопасен на работающей базе: все выражения схемы и миграций идемпотентны.
def init_db(path: Optional[Path] = None) -> None:
    """Создаёт недостающие таблицы и приводит базу к текущей схеме."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# --- Журнал обработки -------------------------------------------------------


# Запись processing появляется до обращения к модели: остановка демона между генерацией
# ответа и его отправкой не приведёт ко второму ответу на то же письмо.
def claim_message(message_id: str) -> bool:
    """Ставит письмо в журнал обработки и сообщает, свободно ли оно было."""
    with connect() as conn:
        # INSERT OR IGNORE вместе с проверкой rowcount выполняет захват одной
        # операцией: два воркера на одном письме получают разные результаты
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed(message_id, status, processed_at) VALUES (?, 'processing', ?)",
            (message_id, now()),
        )
        return cur.rowcount == 1


# вход: Message-ID письма, итоговый статус ('ok' либо 'error') и текст причины.
# побочный эффект: обновление строки в таблице processed
def finish_message(message_id: str, status: str, detail: str = "") -> None:
    """Закрывает заявку на письмо итоговым статусом."""
    with connect() as conn:
        conn.execute(
            "UPDATE processed SET status = ?, detail = ?, processed_at = ? WHERE message_id = ?",
            # detail обрезается до 2000 символов: в него попадает текст
            # исключения вместе с сообщением библиотеки
            (status, detail[:2000], now(), message_id),
        )


# Вызывается при временных сбоях (Exchange недоступен, разрыв сети): письмо, оставленное
# в статусе processing, обработку больше не получит.
def release_message(message_id: str) -> None:
    """Снимает заявку на письмо, возвращая его в очередь следующего прохода."""
    with connect() as conn:
        # условие по статусу защищает завершённую заявку: её удаление привело бы
        # ко второму ответу на письмо
        conn.execute("DELETE FROM processed WHERE message_id = ? AND status = 'processing'", (message_id,))


# Демон, остановленный во время генерации, оставляет заявку в статусе processing;
# такое письмо обработки не получит и в команду retry не попадёт без этого сброса.
def reset_stale_processing(timeout_sec: int) -> int:
    """Помечает ошибкой заявки, зависшие в статусе processing."""
    # порог считается в utc и сравнивается с текстовой колонкой processed_at
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=timeout_sec)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE processed SET status = 'error', detail = 'прервано: демон остановлен во время обработки' "
            "WHERE status = 'processing' AND processed_at < ?",
            (threshold,),
        )
        return cur.rowcount


# выход: строки журнала со статусом error, упорядоченные по времени.
# читает команда cli retry через pipeline.retry_failed
def list_failed() -> List[sqlite3.Row]:
    """Отдаёт письма, обработка которых завершилась ошибкой."""
    with connect() as conn:
        return conn.execute(
            "SELECT message_id, status, detail, processed_at FROM processed "
            "WHERE status = 'error' ORDER BY processed_at"
        ).fetchall()


# После удаления письмо проходит обработку заново.
def forget_message(message_id: str) -> None:
    """Убирает письмо из журнала обработки."""
    with connect() as conn:
        conn.execute("DELETE FROM processed WHERE message_id = ?", (message_id,))


# --- Сессии -----------------------------------------------------------------


# Порядок candidates задаёт приоритет: сессия ближайшего предка возвращается первой.
def find_session_by_message_ids(candidates: Sequence[str], peer_email: str) -> Optional[int]:
    """Ищет сессию треда по идентификаторам писем-предков."""
    # письмо без заголовков треда открывает новую сессию
    if not candidates:
        return None

    with connect() as conn:
        for message_id in candidates:
            # Условие по peer_email обязательно: пересланное письмо сохраняет чужие In-Reply-To/References,
            # и без проверки адреса модель отдала бы новому отправителю контекст чужой переписки.
            row = conn.execute(
                "SELECT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.message_id = ? AND s.peer_email = ?",
                (message_id, peer_email.lower()),
            ).fetchone()

            if row:
                return row["session_id"]
    return None


# Фоллбэк, когда клиент отправителя не проставил заголовки треда; включается флагом THREAD_BY_SUBJECT.
def find_session_by_subject(peer_email: str, title: str) -> Optional[int]:
    """Ищет свежую сессию с той же темой и тем же собеседником."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE peer_email = ? AND title = ? ORDER BY updated_at DESC LIMIT 1",
            (peer_email.lower(), title),
        ).fetchone()
        return row["id"] if row else None


# Message-ID открывшего письма здесь не хранится (он приходит первой репликой в messages):
# отдельная колонка держала бы те же данные во втором месте.
def create_session(title: str, peer_email: str) -> int:
    """Заводит сессию для нового треда писем."""
    # одна метка времени идёт в created_at и updated_at: новая сессия считается
    # свежей при поиске по теме
    stamp = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(title, peer_email, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, peer_email.lower(), stamp, stamp),
        )
        return int(cur.lastrowid)


# вход: идентификатор сессии.
# выход: строка таблицы sessions либо None
def get_session(session_id: int) -> Optional[sqlite3.Row]:
    """Читает сессию по её идентификатору."""
    with connect() as conn:
        return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


# выход: строки сессий с числом реплик, свежие первыми.
# читает команда cli sessions
def list_sessions() -> List[sqlite3.Row]:
    """Отдаёт список сессий со счётчиком реплик в каждой."""
    with connect() as conn:
        # LEFT JOIN оставляет в выборке сессию без реплик, COUNT по ней даёт 0
        return conn.execute(
            "SELECT s.id, s.title, s.peer_email, s.updated_at, COUNT(m.id) AS message_count "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
            "GROUP BY s.id ORDER BY s.updated_at DESC"
        ).fetchall()


# --- Сообщения --------------------------------------------------------------


# Побочные эффекты: строка в messages и обновление updated_at сессии.
def add_message(
    session_id: int, role: str, body: str, message_id: Optional[str], body_raw: str = ""
) -> None:
    """Записывает реплику сессии."""
    stamp = now()
    with connect() as conn:
        # INSERT OR IGNORE гасит повторную вставку после сбоя отправки, когда письмо разбирается заново.
        conn.execute(
            "INSERT OR IGNORE INTO messages(session_id, role, body, body_raw, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            # пустое значение body_raw пишется как NULL: колонка заполняется
            # при STORE_RAW_BODY=true
            (session_id, role, body, body_raw or None, message_id, stamp),
        )

        # отметка времени сессии обновляется и при пропущенной вставке: письмо
        # затронуло сессию, и поиск по теме учитывает свежесть
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))


# Реплики до сводки не возвращаются никогда — иначе контекст удвоился бы её содержанием.
# limit по умолчанию отдаёт всю историю после сводки: отбор под окно модели делает summarizer.py, не эта функция.
def get_history(session_id: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Читает контекст сессии: сводку и реплики после неё."""
    with connect() as conn:
        summary = _latest_summary(conn, session_id)

        # свёрнутые реплики отсекаются по идентификатору: он монотонно растёт
        # вместе с порядком писем, и сравнение по нему не зависит от времени
        floor = summary["covers_upto"] if summary else 0

        columns = "SELECT id, role, body, body_raw, created_at FROM messages WHERE session_id = ? AND id > ?"

        # ветка без ограничения: выборка сразу в хронологическом порядке
        if not limit:
            rows = conn.execute(f"{columns} ORDER BY id", (session_id, floor)).fetchall()
        else:
            # ORDER BY id DESC с LIMIT: по возрастанию отрезало бы limit самых старых реплик; reversed восстанавливает хронологию.
            rows = list(reversed(
                conn.execute(f"{columns} ORDER BY id DESC LIMIT ?", (session_id, floor, limit)).fetchall()
            ))

    context = [dict(row) for row in rows]

    # сводка открывает контекст: она заменяет собой всё, что было до неё
    if summary:
        context.insert(0, summary_row(summary))

    return context


# Значение служит границей свёртки: сводка покрывает реплики по это число включительно.
def last_message_id(session_id: int) -> int:
    """Отдаёт идентификатор последней реплики сессии."""
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS last FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["last"] or 0)


# Читает команда cli history: пользователю показывается переписка целиком, включая свёрнутое в сводку.
def list_messages(session_id: int) -> List[Dict[str, Any]]:
    """Читает реплики сессии целиком, не пропуская свёрнутые."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, body, body_raw, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# --- Сводки сессии ----------------------------------------------------------


# Сводок у сессии бывает несколько: прежние остаются в базе — по ним видно, как сжималась переписка.
def _latest_summary(conn: sqlite3.Connection, session_id: int) -> Optional[sqlite3.Row]:
    """Читает свежую сводку сессии в открытом соединении."""
    return conn.execute(
        "SELECT id, body, covers_upto, covers_chars, reason, created_at FROM summaries "
        "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()


# Роль SUMMARY_ROLE отличает сводку от реплик при сборке запроса и печати переписки; id берётся
# из covers_upto, чтобы следующая свёртка считала границу даже без реплик после сводки.
def summary_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Приводит сводку к форме реплики истории."""
    return {
        "id": row["covers_upto"],
        "role": SUMMARY_ROLE,
        "body": row["body"],
        "body_raw": None,
        "created_at": row["created_at"],
    }


# вход: идентификатор сессии.
# выход: строка последней сводки либо None
def get_summary(session_id: int) -> Optional[sqlite3.Row]:
    """Читает свежую сводку сессии."""
    with connect() as conn:
        return _latest_summary(conn, session_id)


# С этого момента реплики по covers_upto включительно из контекста уходят — их отсекает get_history.
def add_summary(
    session_id: int, body: str, covers_upto: int, covers_chars: int, reason: str
) -> int:
    """Записывает сводку переписки и сдвигает границу контекста сессии."""
    stamp = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO summaries(session_id, body, covers_upto, covers_chars, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, body, covers_upto, covers_chars, reason, stamp),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))
        return int(cur.lastrowid)


# Считаются реплики роли user во всех сессиях адреса; сравнивает с RATE_LIMIT_PER_HOUR pipeline.py.
def count_messages_last_hour(peer_email: str) -> int:
    """Считает письма адресата за последний час для ограничения частоты."""
    threshold = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.peer_email = ? AND m.role = 'user' AND m.created_at >= ?",
            (peer_email.lower(), threshold),
        ).fetchone()
        return int(row["n"])


# --- Срок хранения и удаление ----------------------------------------------


# Запрос выполняется до purge_older_than: после DELETE идентификаторы удалённых строк
# взять было бы уже неоткуда, а они нужны pipeline.run_retention для снятия замков.
def session_ids_older_than(days: int = RETENTION_DAYS) -> List[int]:
    """Отдаёт идентификаторы сессий старше срока хранения, до их удаления."""
    if days <= 0:
        return []

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute("SELECT id FROM sessions WHERE updated_at < ?", (threshold,)).fetchall()
    return [row["id"] for row in rows]


# Удаление строк из sessions и processed; реплики и ссылки на файлы уходят каскадом.
def purge_older_than(days: int = RETENTION_DAYS) -> Tuple[int, int]:
    """Удаляет переписку старше указанного срока хранения."""
    # значение 0 и меньше включает бессрочное хранение
    if days <= 0:
        return (0, 0)

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        # Порог по updated_at сессии, а не по датам отдельных реплик — иначе разорвало бы живой диалог.
        sessions = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (threshold,)).rowcount

        # Журнал чистится тем же порогом, иначе список Message-ID копился бы бессрочно; строки
        # processing остаются — заявка ещё в работе.
        journal = conn.execute(
            "DELETE FROM processed WHERE processed_at < ? AND status != 'processing'", (threshold,)
        ).rowcount

    if sessions or journal:
        log.info("удалено по сроку хранения (%d дней): сессий %d, записей журнала %d", days, sessions, journal)
    return (sessions, journal)


# Удаление строки каскадом уносит её реплики и файлы.
def delete_session(session_id: int) -> int:
    """Удаляет одну сессию со всеми её репликами."""
    with connect() as conn:
        return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount


# Реализует удаление данных по требованию человека, команда cli forget.
def delete_sessions_by_address(peer_email: str) -> int:
    """Удаляет всю переписку с указанным адресом."""
    with connect() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE peer_email = ?", (peer_email.lower().strip(),)
        ).rowcount


# --- Вложения: текст документа остаётся на стороне Open WebUI, поэтому строка без файла смысла
# не имеет — отсюда колонка deleted_at и уборка по сроку хранения. -------------------------------


# Побочный эффект: строка в таблице session_files.
def add_session_file(
    session_id: int,
    file_id: str,
    filename: str,
    size_bytes: int = 0,
    message_id: Optional[str] = None,
) -> None:
    """Запоминает за сессией файл, загруженный в Open WebUI."""
    with connect() as conn:
        # INSERT OR IGNORE гасит повтор: колонка file_id объявлена UNIQUE
        conn.execute(
            "INSERT OR IGNORE INTO session_files"
            "(session_id, file_id, filename, bytes, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, file_id, filename, size_bytes, message_id, now()),
        )


# deleted_at IS NULL обязательно: удалённого по сроку файла в Open WebUI уже нет, ссылка на него в запросе к модели даст ошибку.
def get_session_files(session_id: int, limit: int) -> List[sqlite3.Row]:
    """Читает файлы сессии, доступные для запроса к модели."""
    with connect() as conn:
        return conn.execute(
            "SELECT file_id, filename, bytes FROM session_files "
            "WHERE session_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()


# Письмо разбирается второй раз после сбоя отправки: без проверки каждый повтор клал бы
# в Open WebUI ещё одну копию документа с новым идентификатором.
def filenames_for_message(message_id: str) -> set:
    """Отдаёт имена вложений, загруженных при прошлом разборе этого письма."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT filename FROM session_files WHERE message_id = ?", (message_id,)
        ).fetchall()
    return {row["filename"] for row in rows}


# выход: строки всех файлов, включая удалённые, свежие первыми.
# читает команда cli files
def list_session_files() -> List[sqlite3.Row]:
    """Отдаёт список всех документов, загруженных из писем."""
    with connect() as conn:
        # LEFT JOIN оставляет в выборке файл удалённой сессии, peer_email
        # у такой строки приходит пустым
        return conn.execute(
            "SELECT f.file_id, f.filename, f.bytes, f.created_at, f.deleted_at, "
            "f.session_id, s.peer_email FROM session_files f "
            "LEFT JOIN sessions s ON s.id = f.session_id ORDER BY f.id DESC"
        ).fetchall()


# Список передаётся в owui_files.forget.
def list_expired_files(days: int) -> List[sqlite3.Row]:
    """Отбирает файлы, которым пора уходить из Open WebUI."""
    # значение 0 и меньше отключает уборку файлов
    if days <= 0:
        return []

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        # отсчёт по created_at файла: срок хранения файла отделён от срока
        # хранения переписки
        return conn.execute(
            "SELECT file_id, filename FROM session_files "
            "WHERE deleted_at IS NULL AND created_at < ?",
            (threshold,),
        ).fetchall()


# Строка сохраняется (видно наличие документа в треде); deleted_at IS NULL исключает повторный запрос на удаление.
def mark_file_deleted(file_id: str) -> None:
    """Отмечает, что файла в Open WebUI больше нет."""
    with connect() as conn:
        conn.execute(
            "UPDATE session_files SET deleted_at = ? WHERE file_id = ? AND deleted_at IS NULL",
            (now(), file_id),
        )


# Вызывается перед удалением сессий: каскад унесёт строки session_files, и после список собрать будет неоткуда.
def file_ids_of_sessions(session_ids: Sequence[int]) -> List[str]:
    """Собирает файлы перечисленных сессий до их удаления."""
    if not session_ids:
        return []

    # плейсхолдеры под IN строятся по числу идентификаторов: подстановка
    # значений в текст запроса открыла бы sql-инъекцию
    marks = ",".join("?" * len(session_ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT file_id FROM session_files WHERE deleted_at IS NULL AND session_id IN ({marks})",
            tuple(session_ids),
        ).fetchall()
    return [row["file_id"] for row in rows]


# Отдельный запрос нужен из-за каскада: он унесёт строки session_files, и файлы остались бы в Open WebUI без ссылок.
def file_ids_of_expired_sessions(days: int) -> List[str]:
    """Собирает файлы сессий, которые удалит очистка по сроку хранения."""
    if days <= 0:
        return []

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        # порог применяется к updated_at сессии, тот же критерий использует
        # purge_older_than
        rows = conn.execute(
            "SELECT f.file_id FROM session_files f JOIN sessions s ON s.id = f.session_id "
            "WHERE f.deleted_at IS NULL AND s.updated_at < ?",
            (threshold,),
        ).fetchall()
    return [row["file_id"] for row in rows]


# Признак живой строки — пустой deleted_at; используется командой reconcile для сверки базы с хранилищем.
def all_file_states() -> List[Tuple[str, bool]]:
    """Читает идентификаторы всех известных файлов и их состояние."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT file_id, deleted_at FROM session_files"
        ).fetchall()
    return [(row["file_id"], row["deleted_at"] is None) for row in rows]


# Вызывается командой cli forget перед сбором файлов и удалением сессий.
def find_sessions_by_address(peer_email: str) -> List[int]:
    """Находит идентификаторы всех сессий указанного адреса."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE peer_email = ?", (peer_email.lower().strip(),)
        ).fetchall()
    return [row["id"] for row in rows]


# выход: путь к файлу базы.
# поднимает RuntimeError при отсутствии таблиц схемы
def health_check() -> str:
    """Проверяет доступность базы и наличие таблиц для команды check."""
    # соединение открывается напрямую, минуя connect: проверка читает базу
    # в её текущем состоянии и права на файл не меняет
    with closing(sqlite3.connect(DB_PATH)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # разность множеств даёт таблицы, которых в базе нет
    missing = {"sessions", "messages", "processed", "session_files"} - tables
    if missing:
        raise RuntimeError(f"в базе нет таблиц: {', '.join(sorted(missing))}")

    return str(DB_PATH)
