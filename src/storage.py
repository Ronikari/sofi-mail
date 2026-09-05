"""Хранилище сессий, сообщений и журнала обработки (SQLite).

Четыре таблицы решают четыре разные задачи:
  sessions      — «чат» = тред писем с одним адресатом;
  messages      — реплики в сессии; message_id связывает реплику с письмом
                  и служит якорем для сопоставления будущих Reply;
  processed     — журнал писем, гарантирующий ровно один ответ на письмо;
  session_files — вложения, загруженные в Open WebUI: их id нужен, чтобы
                  follow-up письмо треда спрашивало про тот же документ,
                  а не грузило его копию.
"""

import logging
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from src.config import DB_PATH, RETENTION_DAYS

log = logging.getLogger(__name__)

# В базе лежит переписка должностных лиц целиком. С правами по умолчанию
# (каталог 0755, файл 0644) её читает любой пользователь сервера одной
# командой sqlite3 — поэтому права задаются явно, а не отдаются на волю umask.
# Каталог 0700 важнее файла: он закрывает и файлы WAL (-wal, -shm), которые
# SQLite создаёт сама и на права которых мы напрямую не влияем.
_DIR_MODE = 0o700
_FILE_MODE = 0o600

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
    pages        INTEGER NOT NULL DEFAULT 0,
    chars        INTEGER NOT NULL DEFAULT 0,
    full_context INTEGER NOT NULL DEFAULT 0,
    outline      TEXT,
    message_id   TEXT,
    created_at   TEXT NOT NULL,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_files_session ON session_files(session_id, id);
CREATE INDEX IF NOT EXISTS idx_session_files_alive ON session_files(deleted_at, created_at);
"""


def now() -> str:
    """Единый формат отметок времени: UTC ISO-8601, сортируемый как строка."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _restrict_permissions(db_path: Path) -> None:
    """Убрать у базы и её каталога доступ для всех, кроме владельца.

    Права проверяются на каждом подключении, а не только при создании: база
    могла приехать из бэкапа, быть распакована из архива или создана прежней
    версией демона — и тогда она осталась бы читаемой всем. Лишний stat на фоне
    открытия соединения и PRAGMA ничего не стоит.

    `mkdir(mode=...)` в connect() задаёт права только новому каталогу и вдобавок
    режется umask, поэтому существующий каталог приводится к нужному виду здесь.
    Ошибки прав не считаются фатальными: на смонтированном по сети хранилище
    chmod может быть запрещён, и падать из-за этого демон не должен — но в лог
    это попадает предупреждением, потому что защита в таком случае не работает.
    """
    for target, mode in ((db_path.parent, _DIR_MODE), (db_path, _FILE_MODE)):
        try:
            if not target.exists():
                continue
            current = stat.S_IMODE(target.stat().st_mode)
            if current & ~mode:
                os.chmod(target, mode)
                log.info("права на %s ужесточены: %o -> %o", target, current, mode)
        except OSError as exc:
            log.warning(
                "не удалось ограничить права на %s (%s): переписка может быть "
                "доступна другим пользователям сервера", target, exc
            )


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Соединение с включённым WAL.

    WAL нужен, чтобы `sessions`/`history` можно было смотреть в соседнем
    терминале, пока демон пишет в ту же базу, — без него читатель ловит
    "database is locked" на время записи.
    """
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    conn = sqlite3.connect(db_path, timeout=10)
    # только после connect: сам файл базы создаёт SQLite, и до этого момента
    # ужесточать права было бы не на чем — новая база осталась бы с 0644
    _restrict_permissions(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Колонки, добавленные к таблицам уже после того, как база завелась в бою:
# `CREATE TABLE IF NOT EXISTS` их не доставит — таблица уже есть, и скрипт
# схемы для неё не делает ничего. Список ведётся здесь, а рядом со
# схемой стоит та же колонка, чтобы новая база создавалась сразу правильной.
#
# Почему так, а не нумерованные миграции по PRAGMA user_version: схема тут
# декларативная и накатывается на каждом старте, а база живёт не в одном
# экземпляре — её разворачивают из бэкапа, копируют со стенда, заводят заново.
# Номер версии в такой базе может соврать (бэкап снят до правки, а версия
# записана), и тогда миграция молча не выполнится. Наличие колонки не врёт.
_ADDED_COLUMNS = (
    # объём документа в знаках: страницы у форматов без пагинации условны
    # (см. src/attachments.py), а объём — то, чем документ на самом деле велик
    ("session_files", "chars", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Дотянуть существующую базу до текущей схемы.

    Идемпотентна и молчалива, когда добавлять нечего: вызывается на каждом
    `init_db`, то есть при каждом запуске демона и каждой команде CLI.
    """
    for table, column, ddl in _ADDED_COLUMNS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns:  # страховка: таблицы нет — дополнять нечего
            continue
        if column not in columns:
            log.info("миграция базы: %s.%s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(path: Optional[Path] = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# --- Журнал обработки -------------------------------------------------------


def claim_message(message_id: str) -> bool:
    """Застолбить письмо за собой. True — обрабатываем, False — уже занято.

    Это, а не признак прочитанности письма, гарантирует ровно один ответ: запись появляется
    ДО вызова LLM, поэтому падение между генерацией и отправкой не приведёт
    к повторному ответу после перезапуска демона.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed(message_id, status, processed_at) VALUES (?, 'processing', ?)",
            (message_id, now()),
        )
        return cur.rowcount == 1


def finish_message(message_id: str, status: str, detail: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE processed SET status = ?, detail = ?, processed_at = ? WHERE message_id = ?",
            (status, detail[:2000], now(), message_id),
        )


def release_message(message_id: str) -> None:
    """Снять заявку на письмо, чтобы оно обработалось в следующем проходе.

    Нужно при сбоях, которые лечатся сами собой (Exchange недоступен, сеть упала):
    оставлять письмо в 'processing' — значит потерять его навсегда.
    """
    with connect() as conn:
        conn.execute("DELETE FROM processed WHERE message_id = ? AND status = 'processing'", (message_id,))


def reset_stale_processing(timeout_sec: int) -> int:
    """Пометить зависшие в 'processing' записи как ошибочные.

    Демон, убитый в момент генерации, оставляет заявку висеть; без этой уборки
    письмо не будет обработано никогда и не попадёт в `retry`.
    """
    threshold = (datetime.now(timezone.utc) - timedelta(seconds=timeout_sec)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE processed SET status = 'error', detail = 'прервано: демон остановлен во время обработки' "
            "WHERE status = 'processing' AND processed_at < ?",
            (threshold,),
        )
        return cur.rowcount


def list_failed() -> List[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT message_id, status, detail, processed_at FROM processed "
            "WHERE status = 'error' ORDER BY processed_at"
        ).fetchall()


def forget_message(message_id: str) -> None:
    """Убрать письмо из журнала — команда `retry` обработает его заново."""
    with connect() as conn:
        conn.execute("DELETE FROM processed WHERE message_id = ?", (message_id,))


# --- Сессии -----------------------------------------------------------------


def find_session_by_message_ids(candidates: Sequence[str], peer_email: str) -> Optional[int]:
    """Сессия по Message-ID предков письма; порядок candidates = приоритет.

    Сессия отдаётся, только если она принадлежит тому же собеседнику. Заголовки
    треда доверия не заслуживают: In-Reply-To и References ставит почтовый клиент
    отправителя, и они уезжают дальше вместе с письмом. Стоит переслать ответ
    модели коллеге, а тому нажать «Ответить всем» — и его письмо, придя со своего
    адреса, попало бы в чужую сессию. Модель получила бы в контексте всю прежнюю
    переписку, а ответ по ней ушёл бы новому отправителю.

    Без совпадения адреса письмо начинает новую сессию: потерять склейку треда
    не страшно, отдать чужую переписку — страшно.
    """
    if not candidates:
        return None
    with connect() as conn:
        for message_id in candidates:
            row = conn.execute(
                "SELECT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.message_id = ? AND s.peer_email = ?",
                (message_id, peer_email.lower()),
            ).fetchone()
            if row:
                return row["session_id"]
    return None


def find_session_by_subject(peer_email: str, title: str) -> Optional[int]:
    """Самая свежая сессия с той же темой и тем же собеседником."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE peer_email = ? AND title = ? ORDER BY updated_at DESC LIMIT 1",
            (peer_email.lower(), title),
        ).fetchone()
        return row["id"] if row else None


def create_session(title: str, peer_email: str) -> int:
    """Завести сессию.

    Message-ID письма, открывшего сессию, здесь не дублируется: оно и так
    приезжает первой репликой в `messages`, откуда его берёт и поиск сессии
    по заголовкам треда, и запрос «с какого письма всё началось»
    (`ORDER BY id LIMIT 1`). Отдельная колонка была бы вторым местом хранения
    того же адресного идентификатора — лишние ПДн без единого читателя.
    """
    stamp = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(title, peer_email, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, peer_email.lower(), stamp, stamp),
        )
        return int(cur.lastrowid)


def get_session(session_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def list_sessions() -> List[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT s.id, s.title, s.peer_email, s.updated_at, COUNT(m.id) AS message_count "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
            "GROUP BY s.id ORDER BY s.updated_at DESC"
        ).fetchall()


# --- Сообщения --------------------------------------------------------------


def add_message(
    session_id: int, role: str, body: str, message_id: Optional[str], body_raw: str = ""
) -> None:
    """Записать реплику.

    OR IGNORE, а не обычный INSERT: после сбоя отправки письмо возвращается
    в очередь и разбирается заново — второй копии вопроса в истории быть
    не должно (message_id уникален).
    """
    stamp = now()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO messages(session_id, role, body, body_raw, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, body, body_raw or None, message_id, stamp),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))


def get_history(session_id: int, limit: int) -> List[sqlite3.Row]:
    """Последние `limit` реплик сессии в хронологическом порядке."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, body, body_raw, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return list(reversed(rows))


def count_messages_last_hour(peer_email: str) -> int:
    """Сколько писем прислал адресат за последний час — для rate limit."""
    threshold = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.peer_email = ? AND m.role = 'user' AND m.created_at >= ?",
            (peer_email.lower(), threshold),
        ).fetchone()
        return int(row["n"])


# --- Срок хранения и удаление ----------------------------------------------


def purge_older_than(days: int = RETENTION_DAYS) -> Tuple[int, int]:
    """Удалить переписку старше `days` дней. Возвращает (сессий, записей журнала).

    Отсчёт идёт по `updated_at` сессии, а не по дате отдельных реплик: живой
    диалог не должен рассыпаться на середине из-за того, что первые письма
    в нём старше срока. Реплики уходят каскадом (ON DELETE CASCADE + PRAGMA
    foreign_keys=ON в connect).

    days <= 0 — хранить бессрочно, ничего не делаем.

    Журнал обработки чистится тем же порогом. Формально это ослабляет защиту
    от повторного ответа, но письмо той же давности уже помечено прочитанным
    и в выборку непрочитанных не попадает, а держать вечный список Message-ID
    переписки — то же накопление данных, от которого мы и уходим.
    """
    if days <= 0:
        return (0, 0)

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        sessions = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (threshold,)).rowcount
        journal = conn.execute(
            "DELETE FROM processed WHERE processed_at < ? AND status != 'processing'", (threshold,)
        ).rowcount
    if sessions or journal:
        log.info("удалено по сроку хранения (%d дней): сессий %d, записей журнала %d", days, sessions, journal)
    return (sessions, journal)


def delete_session(session_id: int) -> int:
    """Удалить одну сессию со всеми репликами. Возвращает число удалённых сессий."""
    with connect() as conn:
        return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount


def delete_sessions_by_address(peer_email: str) -> int:
    """Удалить всю переписку с адресом — реализация права на удаление данных."""
    with connect() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE peer_email = ?", (peer_email.lower().strip(),)
        ).rowcount


# --- Вложения ---------------------------------------------------------------
# В таблице лежит не содержимое документа, а ссылка на него в Open WebUI плюс
# то, чем этот документ описать модели в следующем письме треда: имя, объём
# в страницах, режим подачи и оглавление. Сам текст остаётся на той стороне,
# поэтому строка без файла бесполезна — отсюда `deleted_at` и уборка по сроку.


def add_session_file(
    session_id: int,
    file_id: str,
    filename: str,
    pages: int,
    full_context: bool,
    outline: str = "",
    message_id: Optional[str] = None,
    chars: int = 0,
) -> None:
    """Запомнить загруженный файл за сессией."""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO session_files"
            "(session_id, file_id, filename, pages, chars, full_context, outline, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, file_id, filename, pages, chars, int(full_context),
             outline or None, message_id, now()),
        )


def get_session_files(session_id: int, limit: int) -> List[sqlite3.Row]:
    """Живые файлы сессии, свежие первыми.

    Удалённые по сроку хранения не отдаются: файла в Open WebUI уже нет,
    и ссылка на него в запросе привела бы к ошибке вместо ответа.
    """
    with connect() as conn:
        return conn.execute(
            "SELECT file_id, filename, pages, chars, full_context, outline FROM session_files "
            "WHERE session_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()


def filenames_for_message(message_id: str) -> set:
    """Имена вложений, уже загруженных для этого письма.

    Письмо разбирается второй раз после сбоя отправки (см. `release_message`),
    и без этой проверки каждый повтор клал бы в Open WebUI ещё одну копию
    документа — с новым id, которого никто не ждёт, и без единого способа
    отличить её от нужной.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT filename FROM session_files WHERE message_id = ?", (message_id,)
        ).fetchall()
    return {row["filename"] for row in rows}


def list_session_files() -> List[sqlite3.Row]:
    """Все файлы всех сессий — для команды `files`."""
    with connect() as conn:
        return conn.execute(
            "SELECT f.file_id, f.filename, f.pages, f.chars, f.full_context, f.created_at, f.deleted_at, "
            "f.session_id, s.peer_email FROM session_files f "
            "LEFT JOIN sessions s ON s.id = f.session_id ORDER BY f.id DESC"
        ).fetchall()


def list_expired_files(days: int) -> List[sqlite3.Row]:
    """Файлы, которым пора уходить из Open WebUI."""
    if days <= 0:
        return []
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            "SELECT file_id, filename FROM session_files "
            "WHERE deleted_at IS NULL AND created_at < ?",
            (threshold,),
        ).fetchall()


def mark_file_deleted(file_id: str) -> None:
    """Отметить, что файла в Open WebUI больше нет.

    Строка остаётся: по ней видно, что документ в этом треде был, и повторная
    попытка удаления не уйдёт в сеть второй раз.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE session_files SET deleted_at = ? WHERE file_id = ? AND deleted_at IS NULL",
            (now(), file_id),
        )


def file_ids_of_sessions(session_ids: Sequence[int]) -> List[str]:
    """Живые файлы перечисленных сессий — собрать ДО удаления самих сессий."""
    if not session_ids:
        return []
    marks = ",".join("?" * len(session_ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT file_id FROM session_files WHERE deleted_at IS NULL AND session_id IN ({marks})",
            tuple(session_ids),
        ).fetchall()
    return [row["file_id"] for row in rows]


def file_ids_of_expired_sessions(days: int) -> List[str]:
    """Файлы сессий, которые вот-вот удалит `purge_older_than`.

    Отдельный запрос нужен потому, что каскад уносит строки `session_files`
    вместе с сессией, и после удаления спросить «что чистить в Open WebUI»
    будет уже не у кого — файлы остались бы там навсегда.
    """
    if days <= 0:
        return []
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.file_id FROM session_files f JOIN sessions s ON s.id = f.session_id "
            "WHERE f.deleted_at IS NULL AND s.updated_at < ?",
            (threshold,),
        ).fetchall()
    return [row["file_id"] for row in rows]


def find_sessions_by_address(peer_email: str) -> List[int]:
    """Идентификаторы сессий адреса — нужны `forget` до удаления."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE peer_email = ?", (peer_email.lower().strip(),)
        ).fetchall()
    return [row["id"] for row in rows]


def health_check() -> str:
    """Строка для команды `check`: база доступна и схема на месте."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"sessions", "messages", "processed", "session_files"} - tables
    if missing:
        raise RuntimeError(f"в базе нет таблиц: {', '.join(sorted(missing))}")
    return str(DB_PATH)
