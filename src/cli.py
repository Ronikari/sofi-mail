# единая точка входа проекта: демон и отладочные команды под одной cli.
# порядок команды: настройка логов -> проверка .env для команд, работающих
# с почтой -> создание таблиц -> вызов нужного модуля -> печать результата.
# вход: аргументы командной строки и переменные окружения через config.py.
# выход: текст в консоль и код возврата; 1 обозначает ошибку.
# обработку писем ведёт pipeline.py, хранение — storage.py, запрос к модели —
# llm.py, вложения — attachments.py и owui_files.py, почту — transport.py,
# разбор .eml — email_parser.py.
# запускается как `python -m src.cli <команда>`.
#
# докстринги команд ниже читает typer и печатает их в тексте --help.
# импорты тяжёлых модулей стоят внутри команд: команды --help, sessions
# и history загрузку langchain и exchangelib пропускают

import logging
from typing import Optional

import typer

app = typer.Typer(add_completion=False, help="Чат с локальной LLM через электронную почту.")

# запомненный уровень подробности: _setup_logging вызывается дважды за запуск
_verbose = False


# выход: объект OptionInfo для параметра команды.
# опция, объявленная только в callback, принимается до имени команды
# (`cli -v serve`), и запись после имени (`cli serve -v`) даёт от click ответ
# «No such option». поэтому опция объявлена и в callback, и в каждой команде,
# а каждому объявлению нужен свой экземпляр OptionInfo
def verbose_option():
    """Создаёт экземпляр опции -v для отдельной команды."""
    return typer.Option(False, "--verbose", "-v", help="Подробный лог (DEBUG).")


# вход: значение опции -v.
# побочный эффект: настройка корневого логгера.
# вызывается дважды за запуск: из callback приложения и из тела команды
def _setup_logging(verbose: bool) -> None:
    """Настраивает уровень и формат записи логов."""
    global _verbose

    # флаг накапливается по обоим вызовам: второй вызов со значением False
    # погасил бы подробный лог, включённый записью `cli -v serve`
    _verbose = _verbose or verbose

    # force=True перенастраивает уже настроенный корневой логгер: без него
    # повторный вызов basicConfig ничего не меняет
    logging.basicConfig(
        level=logging.DEBUG if _verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    # langchain и HTTP-клиенты на DEBUG заливают лог служебными строками
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# callback приложения: выполняется до тела любой команды
@app.callback()
def main(verbose: bool = verbose_option()) -> None:
    _setup_logging(verbose)


# побочный эффект: печать списка проблем и завершение процесса кодом 1.
# вызывается командами, которые открывают почтовый ящик
def _require_config() -> None:
    """Проверяет настройки .env перед работой с почтой."""
    from src.config import validate

    try:
        validate()

    # ошибка конфигурации печатается человеку списком; трассировка
    # пользователю команды ничего не даёт
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


# проверяет по очереди конфигурацию, базу, шлюз модели, разбор вложений,
# файловый api и доступ к ящику.
# выход: код возврата 1 при любой неуспешной проверке
@app.command()
def check(verbose: bool = verbose_option()) -> None:
    """Диагностика: конфиг, база, Open WebUI, доступ к ящику Exchange."""
    _setup_logging(verbose)
    from src import llm, storage
    from src.config import (
        ATTACHMENTS_ENABLED,
        LLM_MODEL,
        MAIL_ADDRESS,
        WORKERS,
        validate,
    )

    # флаг опускается любой неуспешной проверкой; проход при этом продолжается
    # и показывает все проблемы разом
    ok = True

    def probe(name: str, action) -> None:
        nonlocal ok
        try:
            typer.secho(f"  [ok]   {name}: {action()}", fg=typer.colors.GREEN)
        except Exception as exc:
            ok = False
            typer.secho(f"  [FAIL] {name}: {exc}", fg=typer.colors.RED)

    typer.echo(f"Ящик модели: {MAIL_ADDRESS or '(не задан)'}")
    typer.echo(f"Модель: {LLM_MODEL}")

    # лямбда вызывает validate и отдаёт вторым элементом кортежа текст успеха:
    # сама validate возвращает None
    probe("конфиг", lambda: (validate(), "переменные .env на месте")[1])

    # база создаётся до проверки: health_check читает схему существующей базы
    storage.init_db()
    probe("база", storage.health_check)
    probe("модель", llm.check_llm)

    # вложения проверяются при включённой поддержке: при ATTACHMENTS_ENABLED=false
    # файловый api в работе не участвует
    if ATTACHMENTS_ENABLED:
        from src import attachments, owui_files

        probe("проверка вложений", attachments.selftest)
        probe("файлы в Open WebUI", owui_files.describe)

    def probe_mail() -> str:
        from src.transport import get_transport

        transport = get_transport()
        try:
            return transport.describe()
        finally:
            transport.close()

    # пароль не проверяем: при EWS_AUTH=gssapi/sspi/oauth2 его нет и не должно быть,
    # а без него подключение всё равно состоится по билету Kerberos или токену
    probe("почта", probe_mail)

    # число потоков влияет на нагрузку сервера модели, поэтому строка носит
    # характер замечания и на код возврата не влияет
    if WORKERS > 1:
        typer.secho(
            f"  [i]    потоков обработки: {WORKERS}. Проверьте, что столько "
            "одновременных запросов выдерживают Open WebUI и сервер инференса "
            "за ним, иначе письма встанут в очередь на их стороне",
            fg=typer.colors.BLUE,
        )

    if not ok:
        raise typer.Exit(code=1)


# запускает цикл опроса ящика; управление возвращается по сигналу остановки.
# опции interval, dry_run и workers перекрывают значения из .env
@app.command()
def serve(
    interval: Optional[int] = typer.Option(None, "--interval", help="Интервал опроса ящика, секунд."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Генерировать ответ, но не отправлять письмо."),
    workers: Optional[int] = typer.Option(None, "--workers", "-w", help="Сколько писем обрабатывать одновременно."),
    verbose: bool = verbose_option(),
) -> None:
    """Демон: опрашивать ящик и отвечать на письма."""
    _setup_logging(verbose)
    _require_config()
    from src import pipeline, storage
    from src.config import POLL_INTERVAL_SEC, WORKERS

    storage.init_db()

    # значение None у опции означает «взять из .env»
    pipeline.run_forever(
        interval=interval or POLL_INTERVAL_SEC,
        dry_run=dry_run,
        workers=workers or WORKERS,
    )


# выполняет один проход по непрочитанным письмам и печатает счётчики.
# выход: код возврата 1 при непустом счётчике ошибок
@app.command()
def once(
    dry_run: bool = typer.Option(False, "--dry-run", help="Генерировать ответ, но не отправлять письмо."),
    workers: Optional[int] = typer.Option(None, "--workers", "-w", help="Сколько писем обрабатывать одновременно."),
    verbose: bool = verbose_option(),
) -> None:
    """Один проход по непрочитанным письмам — основной инструмент отладки."""
    _setup_logging(verbose)
    _require_config()
    from src import pipeline, storage
    from src.config import WORKERS

    storage.init_db()
    summary = pipeline.run_once(dry_run=dry_run, workers=workers or WORKERS)

    typer.echo(
        f"писем: {summary.fetched}, ответов: {summary.answered}, "
        f"пропущено: {summary.skipped}, ошибок: {summary.failed}"
    )

    # ненулевой код возврата нужен запуску команды из скрипта и из ci
    if summary.failed:
        raise typer.Exit(code=1)


# отправляет вопрос модели напрямую, минуя почту.
# опция --session подмешивает историю указанной сессии
@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Текст вопроса."),
    session_id: Optional[int] = typer.Option(None, "--session", "-s", help="Сессия, чью историю учесть."),
    verbose: bool = verbose_option(),
) -> None:
    """Спросить модель из терминала — проверка LLM и контекста без почты."""
    _setup_logging(verbose)
    from src import llm, storage
    from src.config import MAX_HISTORY_MESSAGES

    storage.init_db()

    # без указания сессии история пустая, и запрос содержит один вопрос.
    # MAX_HISTORY_MESSAGES=0 читает сессию целиком — так же, как это делает
    # pipeline при обработке письма
    history = (
        storage.get_history(session_id, MAX_HISTORY_MESSAGES or None) if session_id else []
    )
    typer.echo(llm.generate(history, prompt))


# печатает таблицу сессий: id, число реплик, время обновления, адрес, тема
@app.command()
def sessions(verbose: bool = verbose_option()) -> None:
    """Список сессий."""
    _setup_logging(verbose)
    from src import storage

    storage.init_db()
    rows = storage.list_sessions()

    # пустая база даёт отдельную строку: заголовок таблицы без строк
    # выглядел бы сбоем
    if not rows:
        typer.echo("сессий пока нет")
        return

    # ширины колонок в шапке совпадают с ширинами в строках ниже
    typer.echo(f"{'id':>4}  {'реплик':>6}  {'обновлена':<20}  {'собеседник':<28}  тема")
    for row in rows:
        # срез [:19] отбрасывает от метки ISO-8601 часовой пояс
        typer.echo(
            f"{row['id']:>4}  {row['message_count']:>6}  {row['updated_at'][:19]:<20}  "
            f"{row['peer_email']:<28}  {row['title']}"
        )


# печатает реплики сессии в хронологическом порядке.
# переписка показывается целиком, включая свёрнутые письма: в базе они
# остались, и по ним разбирают, что попало в сводку. в контекст модели идут
# только сводка и реплики после неё, поэтому свёрнутые помечаются отдельно.
# выход: код возврата 1 при отсутствии такой сессии
@app.command()
def history(
    session_id: int = typer.Argument(..., help="Идентификатор сессии из команды sessions."),
    raw: bool = typer.Option(False, "--raw", help="Тело письма до отсечения цитаты (нужен STORE_RAW_BODY=true)."),
    verbose: bool = verbose_option(),
) -> None:
    """Показать переписку сессии."""
    _setup_logging(verbose)
    from src import storage

    storage.init_db()
    session = storage.get_session(session_id)

    if session is None:
        typer.secho(f"сессия {session_id} не найдена", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(f"Сессия {session_id}: «{session['title']}» с {session['peer_email']}", bold=True)

    summary = storage.get_summary(session_id)

    for row in storage.list_messages(session_id):
        # реплика с идентификатором не больше границы сводки в запрос
        # к модели больше не попадает
        folded = summary is not None and row["id"] <= summary["covers_upto"]
        who = "пользователь" if row["role"] == "user" else "модель"
        mark = " · свёрнуто в сводку" if folded else ""
        color = typer.colors.CYAN if row["role"] == "user" else typer.colors.GREEN

        typer.secho(f"\n[{row['created_at'][:19]}] {who}{mark}:", fg=color, bold=True)

        # колонка body_raw заполнена при STORE_RAW_BODY=true; при пустом
        # значении печатается очищенное тело
        typer.echo(row["body_raw"] if raw and row["body_raw"] else row["body"])

        # сводка печатается на своём месте в переписке: сразу за последней
        # репликой, которую она покрывает
        if summary is not None and row["id"] == summary["covers_upto"]:
            typer.secho(
                f"\n[{summary['created_at'][:19]}] сводка ({summary['reason']}, "
                f"{summary['covers_chars']} символов свёрнуто):",
                fg=typer.colors.YELLOW, bold=True,
            )
            typer.echo(summary["body"])


# отправляет письмо на адрес самого ящика модели.
# побочный эффект: письмо во входящих ящика; следующий проход демона его прочтёт
@app.command(name="send-test")
def send_test(verbose: bool = verbose_option()) -> None:
    """Отправить тестовое письмо самому себе — проверка права Send As и заголовков."""
    _setup_logging(verbose)
    _require_config()
    from src.config import MAIL_ADDRESS
    from src.transport import get_transport

    transport = get_transport()
    try:
        message_id = transport.send_reply(
            to_address=MAIL_ADDRESS,
            subject="Проверка связи",
            body="Тестовое письмо. Если оно пришло — отправка через EWS настроена верно, "
            "и у служебной учётной записи есть право Send As на этот ящик.",
            session_title="проверка",
        )
    finally:
        transport.close()

    typer.secho(f"отправлено на {MAIL_ADDRESS}, Message-ID {message_id}", fg=typer.colors.GREEN)


# снимает признак прочитанности у писем со статусом error и чистит их записи
# журнала; обработку выполнит следующий проход
@app.command()
def retry(verbose: bool = verbose_option()) -> None:
    """Вернуть письма со статусом error в очередь обработки."""
    _setup_logging(verbose)
    _require_config()
    from src import pipeline, storage

    storage.init_db()
    restored = pipeline.retry_failed()
    typer.echo(f"возвращено в очередь: {restored}")


# удаляет сессии старше срока и файлы этих сессий в Open WebUI.
# опция --days перекрывает RETENTION_DAYS, --yes снимает запрос подтверждения
@app.command()
def purge(
    days: Optional[int] = typer.Option(None, "--days", help="Срок хранения; по умолчанию RETENTION_DAYS из .env."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждения."),
    verbose: bool = verbose_option(),
) -> None:
    """Удалить переписку старше срока хранения.

    Демон делает это сам раз в сутки; команда нужна для разовой чистки и для
    площадок, где срок хранения меняют задним числом.
    """
    _setup_logging(verbose)
    from src import storage
    from src.config import RETENTION_DAYS

    # опция сравнивается с None: значение 0 задаётся осознанно и должно дойти
    # до проверки ниже
    limit = days if days is not None else RETENTION_DAYS

    # срок 0 и меньше означает бессрочное хранение; удаление по нему обнулило бы
    # всю базу
    if limit <= 0:
        typer.secho(
            "срок хранения не задан (RETENTION_DAYS=0) — укажите --days явно",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    storage.init_db()

    # abort=True завершает команду при отрицательном ответе
    if not yes:
        typer.confirm(f"Удалить всю переписку старше {limit} дней?", abort=True)

    from src import owui_files

    # файлы сессий снимаются в Open WebUI до удаления самих сессий: каскад
    # унёс бы строки session_files, и удалять их там было бы уже не по чему
    files = owui_files.forget(storage.file_ids_of_expired_sessions(limit))
    sessions, journal = storage.purge_older_than(limit)

    typer.secho(
        f"удалено сессий: {sessions}, записей журнала: {journal}, файлов в Open WebUI: {files}",
        fg=typer.colors.GREEN,
    )


# удаляет одну сессию либо все сессии адреса вместе с их файлами в Open WebUI.
# выход: код возврата 1 при неверном наборе опций и при пустом результате
@app.command()
def forget(
    session_id: Optional[int] = typer.Option(None, "--session", "-s", help="Удалить одну сессию."),
    address: Optional[str] = typer.Option(None, "--address", "-a", help="Удалить всю переписку с адресом."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждения."),
    verbose: bool = verbose_option(),
) -> None:
    """Удалить переписку по требованию — сессию целиком или все сессии адреса.

    Нужна там, где человек просит удалить свои данные: без неё единственным
    способом остаётся правка базы руками.
    """
    _setup_logging(verbose)
    from src import storage

    # сравнение признаков заданности отсекает и пустой набор опций, и обе сразу
    if (session_id is None) == (address is None):
        typer.secho("укажите ровно одно: --session или --address", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    storage.init_db()
    target = f"сессию {session_id}" if session_id is not None else f"всю переписку с {address}"
    if not yes:
        typer.confirm(f"Удалить {target}? Восстановить будет нечем.", abort=True)

    from src import owui_files

    # Файлы удаляются первыми и по той же причине, что в purge: после удаления
    # сессии их id пропадут вместе с ней. Право на удаление данных означает
    # и удаление документов человека из чужого хранилища
    targets = [session_id] if session_id is not None else storage.find_sessions_by_address(address or "")
    files = owui_files.forget(storage.file_ids_of_sessions(targets))

    # ветка выбирается по той же опции, что и сбор файлов выше
    removed = (
        storage.delete_session(session_id)
        if session_id is not None
        else storage.delete_sessions_by_address(address or "")
    )

    # нулевой результат означает опечатку в адресе либо в номере сессии
    if not removed:
        typer.secho("ничего не найдено", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    typer.secho(
        f"удалено сессий: {removed}, файлов в Open WebUI: {files}", fg=typer.colors.GREEN
    )


# печатает таблицу документов: сессия, вес файла, время загрузки, состояние,
# имя файла
@app.command()
def files(verbose: bool = verbose_option()) -> None:
    """Документы, загруженные в Open WebUI из писем.

    Единственное место, где видно, что сервис оставил на чужой стороне:
    в базе лежит ссылка, сам файл — в Open WebUI под сервисной учётной записью.
    """
    _setup_logging(verbose)
    from src import storage
    from src.attachments import size_words

    storage.init_db()
    rows = storage.list_session_files()

    if not rows:
        typer.echo("файлов пока нет")
        return

    typer.echo(
        f"{'сессия':>6}  {'вес':>12}  {'загружен':<20}  {'состояние':<9}  файл"
    )
    for row in rows:
        # заполненная колонка deleted_at означает, что файла в Open WebUI нет
        state = "удалён" if row["deleted_at"] else "в owui"

        # у файлов, записанных до перехода на серверный разбор, вес нулевой:
        # size_words отдаёт для него «объём неизвестен»
        size = size_words(row["bytes"])

        # пустая колонка session_id остаётся от удалённой сессии
        typer.echo(
            f"{row['session_id'] or '—':>6}  {size:>12}  "
            f"{row['created_at'][:19]:<20}  {state:<9}  {row['filename']}"
        )


# сверяет список файлов Open WebUI с таблицей session_files.
# выход: код возврата 1 при найденных расхождениях
@app.command()
def reconcile(
    delete_orphans: bool = typer.Option(
        False, "--delete-orphans", help="Удалить в Open WebUI файлы, которых нет в базе."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждения."),
    verbose: bool = verbose_option(),
) -> None:
    """Сверить документы в Open WebUI с записями в базе.

    Расхождение возникает при сбое между загрузкой файла и записью строки,
    при удалении файла в интерфейсе Open WebUI и при работе с несколькими
    базами на одну сервисную учётную запись.
    """
    _setup_logging(verbose)
    from src import owui_files, storage

    storage.init_db()

    # remote — то, что действительно лежит в хранилище; known — то, что о нём
    # знает база
    remote = dict(owui_files.list_remote())
    known = dict(storage.all_file_states())

    # файл есть в хранилище, строки о нём в базе нет: уборка по сроку хранения
    # и команда forget работают по строкам таблицы и такой файл не тронут
    orphans = sorted(set(remote) - set(known))

    # строка числится живой, файла в хранилище нет: ссылка на него в запросе
    # к модели даст ошибку вместо ответа
    missing = sorted(fid for fid, alive in known.items() if alive and fid not in remote)

    typer.echo(f"файлов в Open WebUI: {len(remote)}, записей в базе: {len(known)}")

    if orphans:
        typer.secho(f"\nбез записи в базе: {len(orphans)}", fg=typer.colors.YELLOW, bold=True)
        for file_id in orphans:
            typer.echo(f"  {file_id}  {remote[file_id]}")

    if missing:
        typer.secho(f"\nв базе живые, в Open WebUI нет: {len(missing)}", fg=typer.colors.RED, bold=True)
        for file_id in missing:
            typer.echo(f"  {file_id}")

    if not orphans and not missing:
        typer.secho("расхождений нет", fg=typer.colors.GREEN)
        return

    # удаление файлов-сирот выполняется только по явному ключу: команда
    # по умолчанию читает состояние и ничего не меняет
    if orphans and delete_orphans:
        if not yes:
            typer.confirm(f"Удалить {len(orphans)} файлов без записи в базе?", abort=True)

        removed = sum(1 for file_id in orphans if owui_files.delete(file_id))
        typer.secho(f"удалено файлов: {removed} из {len(orphans)}", fg=typer.colors.GREEN)

        # остаток означает отказ сервера либо ATTACHMENT_DELETE_ENABLED=false
        if removed < len(orphans):
            typer.secho(
                f"осталось: {len(orphans) - removed} — проверьте ATTACHMENT_DELETE_ENABLED и лог",
                fg=typer.colors.YELLOW,
            )

    raise typer.Exit(code=1)


# удаляет из Open WebUI файлы старше срока и ставит им отметку deleted_at.
# опция --days перекрывает ATTACHMENT_RETENTION_DAYS
@app.command(name="purge-files")
def purge_files(
    days: Optional[int] = typer.Option(None, "--days", help="Срок; по умолчанию ATTACHMENT_RETENTION_DAYS."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждения."),
    verbose: bool = verbose_option(),
) -> None:
    """Удалить из Open WebUI документы старше срока хранения.

    Демон делает это сам раз в сутки; команда нужна для разовой чистки и там,
    где срок меняют задним числом.
    """
    _setup_logging(verbose)
    from src import owui_files, storage
    from src.config import ATTACHMENT_RETENTION_DAYS

    limit = days if days is not None else ATTACHMENT_RETENTION_DAYS

    # срок 0 и меньше отключает уборку файлов
    if limit <= 0:
        typer.secho(
            "срок хранения файлов не задан (ATTACHMENT_RETENTION_DAYS=0) — укажите --days явно",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    storage.init_db()

    # список собирается до подтверждения: в запросе называется число файлов
    expired = storage.list_expired_files(limit)
    if not expired:
        typer.echo("просроченных файлов нет")
        return

    if not yes:
        typer.confirm(f"Удалить {len(expired)} документов старше {limit} дней?", abort=True)

    removed, failed = owui_files.purge_expired(limit)
    typer.secho(f"удалено файлов: {removed}", fg=typer.colors.GREEN)

    # остаток образуют файлы, которые сервер отказался удалить; они попадут
    # в следующую уборку
    if failed:
        typer.secho(
            f"не удалось удалить: {failed} — повтор при следующей уборке",
            fg=typer.colors.YELLOW,
        )


# прогоняет сохранённый .eml через тот же pipeline, что и письмо из ящика.
# опция --show-parsed останавливает работу на разборе, --send разрешает отправку
@app.command(name="ingest-eml")
def ingest_eml(
    path: str = typer.Argument(..., help="Путь к .eml-файлу."),
    show_parsed: bool = typer.Option(False, "--show-parsed", help="Только разбор письма, без LLM и отправки."),
    dry_run: bool = typer.Option(True, "--dry-run/--send", help="По умолчанию ответ не отправляется."),
    verbose: bool = verbose_option(),
) -> None:
    """Прогнать сохранённое письмо через пайплайн — отладка без почтового ящика."""
    _setup_logging(verbose)
    import email
    from pathlib import Path

    from src import storage
    from src.email_parser import parse_email

    # файл читается байтами: кодировку письма определяет сам разбор
    msg = email.message_from_bytes(Path(path).read_bytes())

    # ветка разбора: печатает результат email_parser и завершает команду
    if show_parsed:
        parsed = parse_email(msg)
        typer.secho("тема:", bold=True)
        typer.echo(f"  {parsed.subject}  ->  сессия «{parsed.title}»")
        typer.secho("отправитель:", bold=True)
        typer.echo(f"  {parsed.sender_name} <{parsed.sender}>")
        typer.secho("тред:", bold=True)
        typer.echo(f"  Message-ID: {parsed.message_id}")
        typer.echo(f"  предки: {parsed.ancestor_ids or '—'}")
        # печатается дата из заголовка письма: по ней виден порядок
        # реплик в треде, когда сессия собралась не так, как ожидалось
        typer.echo(f"  дата письма: {parsed.date or '—'}")
        typer.secho("тело после отсечения цитаты:", bold=True)
        typer.echo(parsed.body or "(пусто)")
        return

    from src import pipeline

    storage.init_db()

    # тот же process_email, что вызывает демон: поведение отладки и демона
    # совпадает по построению
    outcome = pipeline.process_email(msg, dry_run=dry_run)
    typer.echo(f"{outcome.status}: {outcome.detail}")


if __name__ == "__main__":
    app()
