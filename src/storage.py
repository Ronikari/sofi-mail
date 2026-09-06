# хранилище сессий, сообщений, журнала обработки и ссылок на вложения (sqlite).
# порядок: connect открывает соединение и ужесточает права -> init_db создаёт
# таблицы и накатывает миграции -> функции модуля выполняют по одному запросу
# на вызов, каждая в своём соединении.
# вход: разобранные письма и ответы модели от pipeline.py, идентификаторы
# файлов от owui_files.py, аргументы команд от cli.py.
# выход: строки sqlite3.Row, счётчики изменённых строк, списки идентификаторов.
# DB_PATH и RETENTION_DAYS импортируются из config.py.
# вызывается из pipeline.py, owui_files.py и cli.py.
#
# четыре таблицы решают четыре задачи:
#   sessions      — тред писем с одним собеседником
#   messages      — реплики сессии; message_id связывает реплику с письмом
#                   и служит якорем при поиске сессии для будущих ответов
#   processed     — журнал писем, дающий ровно один ответ на письмо
#   session_files — вложения, загруженные в Open WebUI; их идентификаторы
#                   позволяют следующему письму треда ссылаться на тот же документ

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

# права на каталог и файл базы. в базе лежит переписка должностных лиц целиком,
# и при значениях по умолчанию (каталог 0755, файл 0644) её читает любой
# пользователь сервера командой sqlite3. значения задаются явно, поскольку
# umask процесса ослабляет права выборочно.
# режим каталога закрывает и файлы журнала WAL (-wal, -shm), которые sqlite
# создаёт сама
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


# выход: метка времени в utc по ISO-8601 с точностью до секунды.
# формат сортируется лексикографически, поэтому сравнения дат в sql работают
# на текстовых колонках
def now() -> str:
    """Отдаёт текущее время в едином для базы текстовом формате."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# вход: путь к файлу базы.
# побочный эффект: вызов chmod на каталоге и файле базы.
# проверка идёт на каждом подключении: база приезжает из бэкапа, распаковывается
# из архива и создаётся прежними версиями демона, и в этих случаях остаётся
# доступной на чтение всем пользователям сервера
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


# вход: путь к базе; None берёт значение DB_PATH из config.py.
# выход: соединение sqlite3 с row_factory=Row.
# побочные эффекты: создание каталога, ужесточение прав, commit при выходе
# из блока with без исключения и закрытие соединения в любом случае
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

    # WAL (журнал упреждающей записи sqlite) допускает чтение параллельно
    # с записью: команды sessions и history работают в соседнем терминале,
    # пока демон пишет в ту же базу
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


# колонки, добавленные к таблицам после первого запуска базы в работу.
# выражение CREATE TABLE IF NOT EXISTS их не создаёт: таблица уже существует,
# и скрипт схемы для неё ничего не выполняет. те же колонки стоят в SCHEMA
# выше, поэтому новая база создаётся сразу полной.
#
# признаком служит наличие колонки в таблице. схему проекта описывает
# декларативный скрипт, он накатывается на каждом старте, база живёт
# в нескольких экземплярах и разворачивается из бэкапа, копируется со стенда,
# заводится заново. номер версии в PRAGMA user_version расходится со схемой
# такой базы (бэкап снят до правки, номер записан), и миграция по нему
# пропускается без сообщения
_ADDED_COLUMNS = (
    # объём документа в знаках: страницы у форматов без пагинации вычисляются
    # из текста (см. attachments.py), объём измеряет документ напрямую
    ("session_files", "chars", "INTEGER NOT NULL DEFAULT 0"),
)


# вход: открытое соединение.
# побочный эффект: ALTER TABLE для недостающих колонок.
# вызывается на каждом init_db, то есть при каждом запуске демона и каждой
# команде cli; при полной схеме запросов на изменение не выполняет
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


# вход: путь к базе; None берёт DB_PATH.
# побочные эффекты: создание файла базы, таблиц и индексов, накат миграций.
# вызов безопасен на работающей базе: все выражения схемы идемпотентны
def init_db(path: Optional[Path] = None) -> None:
    """Создаёт недостающие таблицы и приводит базу к текущей схеме."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# --- Журнал обработки -------------------------------------------------------


# вход: Message-ID письма.
# выход: True для письма, застолблённого этим вызовом; False для письма,
# уже стоящего в журнале.
# побочный эффект: строка со статусом processing в таблице processed.
# запись появляется до обращения к модели, поэтому остановка демона между
# генерацией ответа и его отправкой не приводит ко второму ответу
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


# вход: Message-ID письма.
# побочный эффект: удаление строки со статусом processing.
# вызывается при сбоях, которые проходят сами (Exchange недоступен, разрыв
# сети): письмо, оставленное в статусе processing, обработку больше не получит
def release_message(message_id: str) -> None:
    """Снимает заявку на письмо, возвращая его в очередь следующего прохода."""
    with connect() as conn:
        # условие по статусу защищает завершённую заявку: её удаление привело бы
        # ко второму ответу на письмо
        conn.execute("DELETE FROM processed WHERE message_id = ? AND status = 'processing'", (message_id,))


# вход: timeout_sec — возраст заявки в секундах, после которого она считается
# зависшей.
# выход: число обновлённых строк.
# побочный эффект: смена статуса processing на error.
# демон, остановленный во время генерации, оставляет заявку в статусе
# processing; такое письмо обработки не получит и в команду retry не попадёт
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


# вход: Message-ID письма.
# побочный эффект: удаление строки из таблицы processed.
# после удаления письмо проходит обработку заново
def forget_message(message_id: str) -> None:
    """Убирает письмо из журнала обработки."""
    with connect() as conn:
        conn.execute("DELETE FROM processed WHERE message_id = ?", (message_id,))


# --- Сессии -----------------------------------------------------------------


# вход: candidates — Message-ID предков письма от ближайшего к дальнему
# (IncomingEmail.ancestor_ids); peer_email — адрес отправителя.
# выход: идентификатор сессии либо None.
# порядок candidates задаёт приоритет: сессия ближайшего предка возвращается первой
def find_session_by_message_ids(candidates: Sequence[str], peer_email: str) -> Optional[int]:
    """Ищет сессию треда по идентификаторам писем-предков."""
    # письмо без заголовков треда открывает новую сессию
    if not candidates:
        return None

    with connect() as conn:
        for message_id in candidates:
            # условие по peer_email обязательно: заголовки In-Reply-To
            # и References ставит почтовый клиент отправителя, и они уезжают
            # вместе с пересланным письмом. ответ коллеги, получившего
            # пересланное письмо, пришёл бы со своего адреса и попал бы в чужую
            # сессию: модель получила бы в контексте всю прежнюю переписку,
            # а ответ по ней ушёл бы новому отправителю.
            # при несовпадении адреса письмо открывает новую сессию: потеря
            # склейки треда обходится дешевле выдачи чужой переписки
            row = conn.execute(
                "SELECT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.message_id = ? AND s.peer_email = ?",
                (message_id, peer_email.lower()),
            ).fetchone()

            if row:
                return row["session_id"]
    return None


# вход: адрес собеседника и нормализованная тема письма.
# выход: идентификатор самой свежей подходящей сессии либо None.
# работает фоллбэком, когда клиент отправителя не проставил заголовки треда;
# включается флагом THREAD_BY_SUBJECT в pipeline.py
def find_session_by_subject(peer_email: str, title: str) -> Optional[int]:
    """Ищет свежую сессию с той же темой и тем же собеседником."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE peer_email = ? AND title = ? ORDER BY updated_at DESC LIMIT 1",
            (peer_email.lower(), title),
        ).fetchone()
        return row["id"] if row else None


# вход: название сессии (нормализованная тема) и адрес собеседника.
# выход: идентификатор созданной сессии.
# побочный эффект: строка в таблице sessions.
# Message-ID открывшего письма здесь не хранится: он приходит первой репликой
# в таблицу messages, откуда его читают и поиск сессии по заголовкам треда,
# и запрос первого письма сессии (ORDER BY id LIMIT 1). отдельная колонка
# держала бы те же персональные данные во втором месте
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


# вход: идентификатор сессии, роль ('user' либо 'assistant'), тело реплики,
# Message-ID письма и тело до отсечения цитаты.
# побочные эффекты: строка в таблице messages и обновление updated_at сессии
def add_message(
    session_id: int, role: str, body: str, message_id: Optional[str], body_raw: str = ""
) -> None:
    """Записывает реплику сессии."""
    stamp = now()
    with connect() as conn:
        # INSERT OR IGNORE гасит повторную вставку: после сбоя отправки письмо
        # возвращается в очередь и разбирается заново, а колонка message_id
        # объявлена UNIQUE
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


# вход: идентификатор сессии и число реплик (MAX_HISTORY_MESSAGES).
# выход: реплики в хронологическом порядке, от старых к новым.
# порядок совпадает с тем, который ожидает llm.build_messages
def get_history(session_id: int, limit: int) -> List[sqlite3.Row]:
    """Читает последние реплики сессии."""
    with connect() as conn:
        # выборка идёт с конца (ORDER BY id DESC с LIMIT): порядок по возрастанию
        # отрезал бы limit самых старых реплик
        rows = conn.execute(
            "SELECT role, body, body_raw, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()

    # reversed восстанавливает хронологию
    return list(reversed(rows))


# вход: адрес собеседника.
# выход: число писем от него за последний час.
# считаются реплики роли user во всех сессиях адреса; значение сравнивает
# с RATE_LIMIT_PER_HOUR модуль pipeline.py
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


# вход: days — срок хранения в сутках; 0 и меньше отключает удаление.
# выход: пара (удалено сессий, удалено записей журнала).
# побочный эффект: удаление строк из sessions и processed; реплики и ссылки
# на файлы уходят каскадом
def purge_older_than(days: int = RETENTION_DAYS) -> Tuple[int, int]:
    """Удаляет переписку старше указанного срока хранения."""
    # значение 0 и меньше включает бессрочное хранение
    if days <= 0:
        return (0, 0)

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        # порог применяется к updated_at сессии: отсчёт по датам отдельных
        # реплик разорвал бы живой диалог, первые письма которого старше срока.
        # реплики и строки session_files уходят каскадом по ON DELETE CASCADE
        sessions = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (threshold,)).rowcount

        # журнал чистится тем же порогом. защита от повторного ответа при этом
        # слабеет, письмо той же давности уже помечено прочитанным и в выборку
        # непрочитанных не попадает. бессрочный список Message-ID переписки
        # накапливал бы данные, от которых уходит срок хранения.
        # строки в статусе processing остаются: заявка ещё в работе
        journal = conn.execute(
            "DELETE FROM processed WHERE processed_at < ? AND status != 'processing'", (threshold,)
        ).rowcount

    if sessions or journal:
        log.info("удалено по сроку хранения (%d дней): сессий %d, записей журнала %d", days, sessions, journal)
    return (sessions, journal)


# вход: идентификатор сессии.
# выход: число удалённых сессий, 0 при отсутствии такой сессии.
# побочный эффект: удаление строки и каскадное удаление её реплик и файлов
def delete_session(session_id: int) -> int:
    """Удаляет одну сессию со всеми её репликами."""
    with connect() as conn:
        return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount


# вход: адрес собеседника.
# выход: число удалённых сессий.
# побочный эффект тот же, что у delete_session.
# реализует удаление данных по требованию человека, команда cli forget
def delete_sessions_by_address(peer_email: str) -> int:
    """Удаляет всю переписку с указанным адресом."""
    with connect() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE peer_email = ?", (peer_email.lower().strip(),)
        ).rowcount


# --- Вложения ---------------------------------------------------------------
# в таблице лежит ссылка на документ в Open WebUI и данные для его описания
# модели в следующем письме треда: имя, объём в страницах и знаках, режим
# подачи, оглавление. текст документа остаётся на стороне Open WebUI, поэтому
# строка без файла применения не имеет — отсюда колонка deleted_at и уборка
# по сроку хранения


# вход: идентификатор сессии, идентификатор файла в Open WebUI, имя файла,
# число страниц, режим подачи целиком, текст оглавления, Message-ID письма
# и объём текста в знаках.
# побочный эффект: строка в таблице session_files
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
    """Запоминает за сессией файл, загруженный в Open WebUI."""
    with connect() as conn:
        # INSERT OR IGNORE гасит повтор: колонка file_id объявлена UNIQUE.
        # значение full_context приводится к целому: sqlite хранит булев тип
        # числом, пустое оглавление пишется как NULL
        conn.execute(
            "INSERT OR IGNORE INTO session_files"
            "(session_id, file_id, filename, pages, chars, full_context, outline, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, file_id, filename, pages, chars, int(full_context),
             outline or None, message_id, now()),
        )


# вход: идентификатор сессии и предел числа строк (ATTACHMENT_MAX_SESSION_FILES).
# выход: строки живых файлов сессии, свежие первыми.
# условие deleted_at IS NULL обязательно: удалённого по сроку файла в Open WebUI
# уже нет, и ссылка на него в запросе к модели даёт ошибку
def get_session_files(session_id: int, limit: int) -> List[sqlite3.Row]:
    """Читает файлы сессии, доступные для запроса к модели."""
    with connect() as conn:
        return conn.execute(
            "SELECT file_id, filename, pages, chars, full_context, outline FROM session_files "
            "WHERE session_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()


# вход: Message-ID письма.
# выход: множество имён файлов, уже загруженных для этого письма.
# письмо разбирается второй раз после сбоя отправки (см. release_message),
# и без этой проверки каждый повтор клал бы в Open WebUI ещё одну копию
# документа с новым идентификатором
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
            "SELECT f.file_id, f.filename, f.pages, f.chars, f.full_context, f.created_at, f.deleted_at, "
            "f.session_id, s.peer_email FROM session_files f "
            "LEFT JOIN sessions s ON s.id = f.session_id ORDER BY f.id DESC"
        ).fetchall()


# вход: days — срок хранения файлов в сутках.
# выход: строки живых файлов старше срока; пустой список при days 0 и меньше.
# список передаётся в owui_files.forget
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


# вход: идентификатор файла в Open WebUI.
# побочный эффект: запись метки времени в колонку deleted_at.
# строка таблицы сохраняется: по ней видно наличие документа в треде,
# а условие deleted_at IS NULL исключает повторный сетевой запрос на удаление
def mark_file_deleted(file_id: str) -> None:
    """Отмечает, что файла в Open WebUI больше нет."""
    with connect() as conn:
        conn.execute(
            "UPDATE session_files SET deleted_at = ? WHERE file_id = ? AND deleted_at IS NULL",
            (now(), file_id),
        )


# вход: идентификаторы сессий.
# выход: идентификаторы их живых файлов в Open WebUI.
# вызывается перед удалением сессий: каскад уносит строки session_files вместе
# с сессией, и после удаления список файлов собрать неоткуда
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


# вход: days — тот же срок, с которым вызывается purge_older_than.
# выход: идентификаторы живых файлов сессий, попадающих под удаление.
# отдельный запрос нужен из-за каскада: он уносит строки session_files вместе
# с сессией, и файлы остались бы в Open WebUI без ссылок на них
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


# выход: пары (идентификатор файла, признак живой строки) для всех записей
# таблицы session_files.
# признак живой строки означает пустой deleted_at: файл числится существующим
# в Open WebUI.
# используется командой reconcile для сверки базы с хранилищем
def all_file_states() -> List[Tuple[str, bool]]:
    """Читает идентификаторы всех известных файлов и их состояние."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT file_id, deleted_at FROM session_files"
        ).fetchall()
    return [(row["file_id"], row["deleted_at"] is None) for row in rows]


# вход: адрес собеседника.
# выход: идентификаторы его сессий.
# вызывается командой cli forget перед сбором файлов и удалением сессий
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
