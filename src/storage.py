"""Хранилище сессий, сообщений и журнала обработки (SQLite).

Три таблицы решают три разные задачи:
  sessions  — «чат» = тред писем с одним адресатом;
  messages  — реплики в сессии; message_id связывает реплику с письмом и служит
              якорем для сопоставления будущих Reply;
  processed — журнал писем, гарантирующий ровно один ответ на письмо.
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
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    peer_email      TEXT NOT NULL,
    root_message_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
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


def init_db(path: Optional[Path] = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


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


def create_session(title: str, peer_email: str, root_message_id: Optional[str]) -> int:
    stamp = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(title, peer_email, root_message_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, peer_email.lower(), root_message_id, stamp, stamp),
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


def health_check() -> str:
    """Строка для команды `check`: база доступна и схема на месте."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"sessions", "messages", "processed"} - tables
    if missing:
        raise RuntimeError(f"в базе нет таблиц: {', '.join(sorted(missing))}")
    return str(DB_PATH)
