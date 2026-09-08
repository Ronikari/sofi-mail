# sofi-mail

Чат с локальной LLM через почту. Пользователь пишет письмо в закреплённый за моделью ящик:
тело — промпт, тема — название сессии, вложения уходят в Open WebUI. Демон читает EWS-опросом,
отвечает Reply с `[Sofi]` в первой строке, заголовками `In-Reply-To`/`References`/`Thread-Index`
(Outlook/OWA кладут ответ в тот же разговор). Ответ на ответ продолжает сессию; новая тема —
новая сессия.

```
EWS (polling) → разбор письма → сессия (SQLite) → модель (Open WebUI) → ответ письмом
```

В `messages` — одна цепочка реплик, по строке на письмо; переписка треда не переписывается,
объём запроса растёт линейно. Промпт, параметры генерации, базы знаний и фильтры настраиваются
на модели `sofi-mail` в Open WebUI. История переписки хранится в SQLite, в чаты Open WebUI
не уезжает.

Поставляется одним контейнером: демон + том с базой + смонтированный `.env`.

Разделы: [Сборка](#сборка) · [Настройка](#настройка) · [Запуск](#запуск) · [Команды](#команды) · [Эксплуатация](#эксплуатация) · [Структура](#структура)

## Сборка

```bash
cp .env.example .env      # заполнить перед первым запуском
docker compose build
```

Два слоя: сборка зависимостей → боевой образ с готовым `venv`. Процесс работает под `sofi`
(uid 1000), код принадлежит `root`, доступен на чтение.

### Из Nexus (закрытый контур)

```dotenv
BASE_IMAGE=nexus.company.ru:8083/python:3.12-slim-bookworm
PIP_INDEX_URL=https://nexus.company.ru/repository/pypi-proxy/simple
PIP_TRUSTED_HOST=nexus.company.ru
DEBIAN_MIRROR=https://nexus.company.ru/repository/debian-proxy
APP_UID=1000                          # владелец каталога базы при bind-mount
```

`DEBIAN_MIRROR` нужен только на слое сборки (`build-essential`, заголовки `libxml2`/`libxslt`
под lxml). `PIP_TRUSTED_HOST` отключает проверку сертификата для Nexus с внутренним УЦ.

```bash
docker build --build-arg BASE_IMAGE=nexus.company.ru:8083/python:3.12-slim-bookworm \
  --build-arg PIP_INDEX_URL=https://nexus.company.ru/repository/pypi-proxy/simple \
  -t nexus.company.ru:8083/sofi-mail:1.0 .
docker push nexus.company.ru:8083/sofi-mail:1.0
```

На целевом хосте: `SOFI_IMAGE=nexus.company.ru:8083/sofi-mail:1.0` в `.env`, затем
`docker compose pull && docker compose up -d`.

### Тесты перед push

CI в проекте нет: `docker build --target test .` — единственная автоматическая
проверка, что тестовый образ вообще собирается (тот же базовый образ, те же
системные библиотеки, `requirements.txt` + `requirements-dev.txt`) и что
`pytest` в нём проходит. Без привязки к процессу эта стадия ломается
незаметно — так `requirements-dev.txt` уже пропадал из репозитория
(`sofi-mail-known-issues.md`, п.19/21).

Включить хук `pre-push`, гоняющий эту стадию перед каждым push:

```bash
git config core.hooksPath .githooks
```

Разовый пропуск: `SKIP_DOCKER_TEST=1 git push`.

## Настройка

Обязательные поля `.env`: `MAIL_ADDRESS`, `MAIL_PASSWORD`, `EWS_SERVER`, `LLM_BASE_URL`,
`LLM_API_KEY`, `ALLOWED_SENDERS`. Файл монтируется в контейнер на чтение, в образ не попадает.

### Почтовый ящик Exchange

Отдельный ящик под модель, без интерактивного входа человека (иначе область чтения Outlook
«съедает» непрочитанные до демона). Протокол — EWS.

```dotenv
MAIL_ADDRESS=sofi@so-ups.ru          # ящик модели
MAIL_LOGIN=CORP\svc-llm              # логин служебной учётки (если отличается от адреса)
MAIL_PASSWORD=<пароль служебной учётки>
EWS_SERVER=mail.company.ru           # пусто = autodiscover
EWS_AUTH=ntlm                        # basic | ntlm | digest | oauth2 | gssapi*; пусто = авто
EWS_ACCESS_TYPE=delegate             # impersonation — для входа «от имени» ящика
```

Служебной учётке нужны права `FullAccess` и `Send As` на ящик (независимые права в Exchange):

```powershell
Add-MailboxPermission -Identity sofi@so-ups.ru -User svc-llm -AccessRights FullAccess
Add-ADPermission     -Identity sofi@so-ups.ru -User svc-llm -ExtendedRights "Send As"
Get-ThrottlingPolicy | Select Name, EwsMaxConcurrency   # потолок ограничивает WORKERS
```

По умолчанию `ntlm` + `delegate`: область доступа — один ящик. `EWS_AUTH=oauth2` (Entra ID)
требует регистрации приложения с `full_access_as_app`, ограниченной одним ящиком через
`New-ApplicationAccessPolicy`:

```dotenv
EWS_AUTH=oauth2
EWS_CLIENT_ID=00000000-0000-0000-0000-000000000000
EWS_CLIENT_SECRET=...
EWS_TENANT_ID=...                    # пусто = тенант по умолчанию
MAIL_PASSWORD=                       # пусто = приложение работает от своего имени
EWS_ACCESS_TYPE=impersonation        # обязателен при пустом MAIL_PASSWORD, нужно право ApplicationImpersonation
```

Пара `oauth2` + `delegate` без пароля отклоняется командой `check` на старте; `oauth2` требует
исходящего доступа до `login.microsoftonline.com:443`. `EWS_AUTH=gssapi` в образе не собран.

### Сертификаты внутреннего УЦ

```bash
cp corp-root.pem certs/     # до сборки; каталог копируется в /opt/sofi-mail/certs
```

```dotenv
MAIL_CA_FILE=/opt/sofi-mail/certs/corp-root.pem
LLM_CA_FILE=/opt/sofi-mail/certs/corp-root.pem
```

`certs/` не коммитится. Если сертификат меняется чаще образа — монтировать томом вместо копии.

### Модель (Open WebUI)

Системный промпт, параметры генерации, базы знаний и фильтры задаются на модели `sofi-mail`
в Open WebUI. Демон отправляет только `model` и `messages`.

```dotenv
LLM_BASE_URL=https://sofi.cdu.so/api      # суффикс /api, не /v1
LLM_CA_FILE=/opt/sofi-mail/certs/corp-root.pem
LLM_MODEL=sofi-mail                       # Model ID из Workspace → Models
LLM_API_KEY=sk-...                        # ключ сервисной учётки
LLM_WEB_URL=https://sofi.cdu.so           # пусто = из LLM_BASE_URL без /api
```

Настройка один раз в Open WebUI: сервисная учётная запись → Workspace → Models → New model
(`Model ID = sofi-mail`) → System prompt (без markdown, приветствия и подписи — их ставит демон)
→ Advanced params → Knowledge/Filters → Settings → Account → API keys → `LLM_API_KEY`.

```bash
docker compose run --rm sofi-mail check   # проверка адреса, TLS, Model ID, доступа к ящику
```

`LLM_BASE_URL` обязан быть `https` (кроме `LLM_ALLOW_INSECURE=true`), `LLM_MODEL` сверяется
точным равенством со списком моделей. Контекст сессии не обрезается (`MAX_HISTORY_MESSAGES=0`,
`MAX_PROMPT_CHARS=0`); цитату отсекает `email_parser` по блоку Exchange, метке `[Sofi]` и подписи.
`MAX_CONTEXT_CHARS` — порог для лога (`llm.fit_context`), запрос не режет.

### Суммаризация

При превышении `SESSION_MAX_CHARS` реплики до текущего письма сворачиваются в сводку
(`src/summarizer.py`), которую пишет та же модель; целевая длина — доля
`SUMMARY_COMPRESSION_RATIO` от объёма (`0.2` = впятеро короче). Ручной запуск: `/summary` первой
строкой или явная просьба словами. Сбой суммаризации не отменяет ответ — запрос уходит полным
контекстом, причина в логе.

### Вложения

PDF, docx, pptx, xlsx, doc, xls, ppt, csv, txt, md, html, изображения.

```
письмо → POST /api/v1/files/ → GET …/process/status → files:[{id}] в запросе к модели
```

Разбор документа выполняет Open WebUI (текст извлекается на его стороне). Подача по умолчанию —
фокусированный поиск по документу; подача целиком включается инструментом
`src/tools/full_context_tool.py` (ставится в Workspace → Инструменты, включается у модели).

| Настройка (Valves инструмента) | По умолчанию | Что держит |
| --- | --- | --- |
| `max_chars` | 200 000 | суммарный объём документов в модель |
| `max_file_chars` | 100 000 | предел на один документ |
| `include_names` | true | подписывать документы именами в ответе |

Локальные проверки sofi-mail: вес файла, расширение, пустой файл, архивы отклоняются.

```dotenv
ATTACHMENTS_ENABLED=true             # false — вложения игнорируются с примечанием в ответе
ATTACHMENT_MAX_MB=20
ATTACHMENT_MAX_COUNT=5               # файлов из одного письма
ATTACHMENT_MAX_SESSION_FILES=10      # файлов треда в одном запросе к модели
ATTACHMENT_PROCESS_TIMEOUT_SEC=120
ATTACHMENT_RETENTION_DAYS=30         # срок жизни файла в Open WebUI; 0 — не удалять
```

`file_id` и вес файла хранятся в `session_files` для follow-up писем того же треда.

```bash
docker compose run --rm sofi-mail files                 # что лежит в Open WebUI и от каких сессий
docker compose run --rm sofi-mail purge-files --days 7  # разовая чистка
```

## Запуск

```bash
docker compose run --rm sofi-mail check              # диагностика: конфиг, база, модель, ящик
docker compose run --rm sofi-mail serve --dry-run -v  # первый запуск: печать без отправки
docker compose up -d                                  # рабочий режим
docker compose logs -f
```

Контейнер: read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, запись только в том базы и
tmpfs `/tmp`. `docker compose stop` — SIGTERM, `stop_grace_period` 330с (запас над
`LLM_TIMEOUT_SEC` 300с). После аварийной остановки зависшие письма помечаются `error`,
возврат в очередь — командой `retry`.

`WORKERS=1` по умолчанию, пул потоков — `WORKERS=4` или `-w 4`. Второй демон на тот же ящик
не поднимать — масштабирование через `WORKERS`.

## Команды

`docker compose run --rm sofi-mail <команда>`

| Команда | Назначение |
|---|---|
| `serve` | демон: опрашивает ящик каждые `POLL_INTERVAL_SEC`; `-w N` — потоков |
| `once` | один проход по непрочитанным — инструмент отладки |
| `check` | проверка конфига, базы, модели, доступа к ящику |
| `ask "текст" [-s ID]` | спросить модель из терминала, без почты |
| `sessions` | список сессий: id, число реплик, собеседник, тема |
| `history ID [--raw]` | переписка сессии; `--raw` требует `STORE_RAW_BODY=true` |
| `send-test` | тестовое письмо самому себе |
| `retry` | вернуть письма со статусом `error` в очередь |
| `purge [--days N]` | удалить переписку старше срока хранения |
| `forget --session N \| --address A` | удалить переписку по требованию |
| `files` | документы в Open WebUI и их сессии |
| `purge-files [--days N]` | удалить документы в Open WebUI старше срока |
| `ingest-eml FILE [--show-parsed]` | прогнать сохранённое письмо через пайплайн без почты |

Флаг `-v` — DEBUG-лог, в любой позиции. База в режиме WAL — `sessions`/`history` читаются на
работающем демоне.

## Эксплуатация

**Обновление.** `docker compose pull && docker compose up -d`. Том с базой переживает
пересоздание контейнера, `init_db` создаёт недостающие таблицы; изменение схемы существующих —
вручную (`ALTER TABLE`) до подъёма. Откат — прежним тегом в `SOFI_IMAGE`.

**Бэкап.** Снимок штатным механизмом SQLite на работающем демоне (рядом лежит незакоммиченный WAL):

```bash
mkdir -p backup && sudo chown 1000:1000 backup
docker compose run --rm -v "$PWD/backup:/backup" --entrypoint python sofi-mail -c \
  "import sqlite3; s=sqlite3.connect('/opt/sofi-mail/data/sessions.db'); \
d=sqlite3.connect('/backup/sessions.db'); s.backup(d); d.close(); s.close()"
mv backup/sessions.db backup/sessions-$(date +%F).db
```

**Логи.** stdout → драйвер Docker (ротация 10 МБ × 5). Адреса маскируются, темы писем не
логируются; `LOG_PII=true` — только на время разбора конкретной проблемы.

## Структура

| Модуль | Ответственность |
|---|---|
| `src/config.py` | точка чтения `.env` |
| `src/storage.py` | SQLite: сессии, реплики, сводки, журнал обработанных писем |
| `src/email_parser.py` | разбор MIME, кодировки, отсечение цитат и прошлых ответов, фильтры петель |
| `src/transport.py` | контракт почты: операции, которыми пользуется пайплайн |
| `src/ews_client.py` | приём и отправка через Exchange Web Services |
| `src/reply_builder.py` | сборка ответа: метка `[Sofi]`, заголовки треда и разговора |
| `src/llm.py` | сборка контекста запроса, диагностика бюджета, отсечение рассуждений |
| `src/summarizer.py` | свёртка переписки в сводку |
| `src/llm_backend.py` | обращение к Open WebUI: адрес, ключ, TLS, сверка Model ID |
| `src/attachments.py` | проверка вложений: вес, формат, mime-тип (локально, без сети и БД) |
| `src/attachment_context.py` | загрузка в Open WebUI, запись `session_files`, описания для запроса |
| `src/owui_files.py` | файлы в Open WebUI: загрузка, готовность, удаление, срок хранения |
| `src/redact.py` | маскирование адресов и тем писем в логах |
| `src/pipeline.py` | оркестрация: письмо → сессия → ответ → письмо, пул воркеров |
| `src/cli.py` | Typer, точка входа |

| Файл сборки | Назначение |
|---|---|
| `Dockerfile` | три стадии: зависимости, тесты (`--target test`), боевой образ |
| `docker-compose.yml` | выкладка: том с базой, ограничения прав, ротация логов |
| `.dockerignore` | исключает `.env`, `data/`, историю git из контекста сборки |
| `certs/` | корневые сертификаты внутреннего УЦ, копируются в образ |
