"""Единая точка входа проекта.

Демон и отладочные команды живут под одной CLI, а не расползаются по отдельным
скриптам: `once` и `serve` вызывают один и тот же код обработки письма, поэтому
то, что отлажено вручную, работает и в демоне.

Импорты тяжёлых модулей — внутри команд: `--help` и `sessions` не должны ждать
загрузки langchain.
"""

import logging
from typing import Optional

import typer

app = typer.Typer(add_completion=False, help="Чат с локальной LLM через электронную почту.")

_verbose = False


def verbose_option():
    """Опция -v для каждой команды по отдельности.

    Опция, объявленная только в callback, принимается лишь ДО имени команды
    (`cli -v serve`), а естественнее всего пишется после (`cli serve -v`) — и там
    click отвечал «No such option». Поэтому опция есть и в callback, и в каждой
    команде; функция нужна, чтобы у каждой был свой экземпляр OptionInfo.
    """
    return typer.Option(False, "--verbose", "-v", help="Подробный лог (DEBUG).")


def _setup_logging(verbose: bool) -> None:
    """Настроить логи. Вызывается дважды за запуск: из callback и из команды.

    Флаг запоминается (иначе второй вызов с False погасил бы `cli -v serve`),
    а force=True нужен потому, что повторный basicConfig на уже настроенном
    root-логгере — пустышка.
    """
    global _verbose
    _verbose = _verbose or verbose
    logging.basicConfig(
        level=logging.DEBUG if _verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # langchain и HTTP-клиенты на DEBUG заливают лог служебными строками
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.callback()
def main(verbose: bool = verbose_option()) -> None:
    _setup_logging(verbose)


def _require_config() -> None:
    """Проверка .env перед командами, работающими с почтой."""
    from src.config import validate

    try:
        validate()
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


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
    probe("конфиг", lambda: (validate(), "переменные .env на месте")[1])

    storage.init_db()
    probe("база", storage.health_check)
    probe("модель", llm.check_llm)

    if ATTACHMENTS_ENABLED:
        from src import owui_files

        from src import attachments

        probe("разбор вложений", attachments.selftest)
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

    if WORKERS > 1:
        # клиентский параллелизм бесполезен, если очередь держит сам сервер модели
        typer.secho(
            f"  [i]    потоков обработки: {WORKERS}. Проверьте, что столько "
            "одновременных запросов выдерживают Open WebUI и сервер инференса "
            "за ним, иначе письма встанут в очередь на их стороне",
            fg=typer.colors.BLUE,
        )

    if not ok:
        raise typer.Exit(code=1)


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
    pipeline.run_forever(
        interval=interval or POLL_INTERVAL_SEC,
        dry_run=dry_run,
        workers=workers or WORKERS,
    )


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
    if summary.failed:
        raise typer.Exit(code=1)


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
    history = storage.get_history(session_id, MAX_HISTORY_MESSAGES) if session_id else []
    typer.echo(llm.generate(history, prompt))


@app.command()
def sessions(verbose: bool = verbose_option()) -> None:
    """Список сессий."""
    _setup_logging(verbose)
    from src import storage

    storage.init_db()
    rows = storage.list_sessions()
    if not rows:
        typer.echo("сессий пока нет")
        return

    typer.echo(f"{'id':>4}  {'реплик':>6}  {'обновлена':<20}  {'собеседник':<28}  тема")
    for row in rows:
        typer.echo(
            f"{row['id']:>4}  {row['message_count']:>6}  {row['updated_at'][:19]:<20}  "
            f"{row['peer_email']:<28}  {row['title']}"
        )


@app.command()
def history(
    session_id: int = typer.Argument(..., help="Идентификатор сессии из команды sessions."),
    raw: bool = typer.Option(False, "--raw", help="Тело письма до отсечения цитаты (нужен STORE_RAW_BODY=true)."),
    verbose: bool = verbose_option(),
) -> None:
    """Показать переписку сессии."""
    _setup_logging(verbose)
    from src import storage
    from src.config import MAX_HISTORY_MESSAGES

    storage.init_db()
    session = storage.get_session(session_id)
    if session is None:
        typer.secho(f"сессия {session_id} не найдена", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(f"Сессия {session_id}: «{session['title']}» с {session['peer_email']}", bold=True)
    for row in storage.get_history(session_id, MAX_HISTORY_MESSAGES):
        who = "пользователь" if row["role"] == "user" else "модель"
        color = typer.colors.CYAN if row["role"] == "user" else typer.colors.GREEN
        typer.secho(f"\n[{row['created_at'][:19]}] {who}:", fg=color, bold=True)
        typer.echo(row["body_raw"] if raw and row["body_raw"] else row["body"])


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


@app.command()
def retry(verbose: bool = verbose_option()) -> None:
    """Вернуть письма со статусом error в очередь обработки."""
    _setup_logging(verbose)
    _require_config()
    from src import pipeline, storage

    storage.init_db()
    restored = pipeline.retry_failed()
    typer.echo(f"возвращено в очередь: {restored}")


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

    limit = days if days is not None else RETENTION_DAYS
    if limit <= 0:
        typer.secho(
            "срок хранения не задан (RETENTION_DAYS=0) — укажите --days явно",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    storage.init_db()
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
    # и удаление документов человека из чужого хранилища, а не только из базы
    targets = [session_id] if session_id is not None else storage.find_sessions_by_address(address or "")
    files = owui_files.forget(storage.file_ids_of_sessions(targets))

    removed = (
        storage.delete_session(session_id)
        if session_id is not None
        else storage.delete_sessions_by_address(address or "")
    )
    if not removed:
        typer.secho("ничего не найдено", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    typer.secho(
        f"удалено сессий: {removed}, файлов в Open WebUI: {files}", fg=typer.colors.GREEN
    )


@app.command()
def files(verbose: bool = verbose_option()) -> None:
    """Документы, загруженные в Open WebUI из писем.

    Единственное место, где видно, что сервис оставил на чужой стороне:
    в базе лежит ссылка, сам текст документа — в Open WebUI под сервисной
    учётной записью.
    """
    _setup_logging(verbose)
    from src import storage

    storage.init_db()
    rows = storage.list_session_files()
    if not rows:
        typer.echo("файлов пока нет")
        return

    typer.echo(
        f"{'сессия':>6}  {'стр.':>5}  {'знаков':>8}  {'режим':<8}  "
        f"{'загружен':<20}  {'состояние':<9}  файл"
    )
    for row in rows:
        state = "удалён" if row["deleted_at"] else "в owui"
        mode = "целиком" if row["full_context"] else "поиск"
        # у файлов, загруженных до появления колонки, объём нулевой — прочерк
        # честнее нуля: документ не пустой, просто мы его тогда не записали
        chars = row["chars"] or "—"
        typer.echo(
            f"{row['session_id'] or '—':>6}  {row['pages']:>5}  {chars:>8}  {mode:<8}  "
            f"{row['created_at'][:19]:<20}  {state:<9}  {row['filename']}"
        )


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
    if limit <= 0:
        typer.secho(
            "срок хранения файлов не задан (ATTACHMENT_RETENTION_DAYS=0) — укажите --days явно",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    storage.init_db()
    expired = storage.list_expired_files(limit)
    if not expired:
        typer.echo("просроченных файлов нет")
        return
    if not yes:
        typer.confirm(f"Удалить {len(expired)} документов старше {limit} дней?", abort=True)

    removed, failed = owui_files.purge_expired(limit)
    typer.secho(f"удалено файлов: {removed}", fg=typer.colors.GREEN)
    if failed:
        typer.secho(
            f"не удалось удалить: {failed} — повтор при следующей уборке",
            fg=typer.colors.YELLOW,
        )


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

    msg = email.message_from_bytes(Path(path).read_bytes())

    if show_parsed:
        parsed = parse_email(msg)
        typer.secho("тема:", bold=True)
        typer.echo(f"  {parsed.subject}  ->  сессия «{parsed.title}»")
        typer.secho("отправитель:", bold=True)
        typer.echo(f"  {parsed.sender_name} <{parsed.sender}>")
        typer.secho("тред:", bold=True)
        typer.echo(f"  Message-ID: {parsed.message_id}")
        typer.echo(f"  предки: {parsed.ancestor_ids or '—'}")
        # дата письма, а не время обработки: по ней видно реальный порядок
        # реплик в треде, когда сессия собралась не так, как ожидалось
        typer.echo(f"  дата письма: {parsed.date or '—'}")
        typer.secho("тело после отсечения цитаты:", bold=True)
        typer.echo(parsed.body or "(пусто)")
        return

    from src import pipeline

    storage.init_db()
    outcome = pipeline.process_email(msg, dry_run=dry_run)
    typer.echo(f"{outcome.status}: {outcome.detail}")


if __name__ == "__main__":
    app()
