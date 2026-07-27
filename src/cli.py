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
    # imaplib/langchain на DEBUG заливают лог служебными протокольными строками
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
    """Диагностика: конфиг, база, Ollama, IMAP, SMTP."""
    _setup_logging(verbose)
    from src import llm, storage
    from src.config import MAIL_ADDRESS, MAIL_TRANSPORT, WORKERS, validate

    ok = True

    def probe(name: str, action) -> None:
        nonlocal ok
        try:
            typer.secho(f"  [ok]   {name}: {action()}", fg=typer.colors.GREEN)
        except Exception as exc:
            ok = False
            typer.secho(f"  [FAIL] {name}: {exc}", fg=typer.colors.RED)

    typer.echo(f"Ящик модели: {MAIL_ADDRESS or '(не задан)'} (транспорт: {MAIL_TRANSPORT})")
    probe("конфиг", lambda: (validate(), "переменные .env на месте")[1])

    storage.init_db()
    probe("база", storage.health_check)
    probe("ollama", llm.check_ollama)

    from src.config import MAIL_PASSWORD

    if MAIL_PASSWORD or MAIL_TRANSPORT == "ews":
        def probe_mail() -> str:
            from src.transport import get_transport

            transport = get_transport()
            try:
                return transport.describe()
            finally:
                transport.close()

        probe("почта", probe_mail)
    else:
        typer.secho("  [--]   почта: пропущено, MAIL_PASSWORD не задан", fg=typer.colors.YELLOW)

    if WORKERS > 1:
        # клиентский параллелизм бесполезен, если сама Ollama держит очередь
        typer.secho(
            f"  [i]    потоков обработки: {WORKERS}. Проверьте, что Ollama запущена "
            f"с OLLAMA_NUM_PARALLEL >= {WORKERS}, иначе письма встанут в очередь внутри неё",
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
    raw: bool = typer.Option(False, "--raw", help="Показать тело письма до отсечения цитаты."),
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
    """Отправить тестовое письмо самому себе — проверка SMTP и заголовков."""
    _setup_logging(verbose)
    _require_config()
    from src.config import MAIL_ADDRESS, MAIL_TRANSPORT
    from src.transport import get_transport

    transport = get_transport()
    try:
        message_id = transport.send_reply(
            to_address=MAIL_ADDRESS,
            subject="Проверка связи",
            body=f"Тестовое письмо от llm-email-chat. Если оно пришло — отправка через "
            f"{MAIL_TRANSPORT} настроена верно.",
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
        typer.secho("тело после отсечения цитаты:", bold=True)
        typer.echo(parsed.body or "(пусто)")
        return

    from src import pipeline

    storage.init_db()
    outcome = pipeline.process_email(msg, dry_run=dry_run)
    typer.echo(f"{outcome.status}: {outcome.detail}")


if __name__ == "__main__":
    app()
