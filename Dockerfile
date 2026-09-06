# образ демона sofi-mail.
# порядок сборки: стадия builder ставит зависимости в venv -> стадия test
# прогоняет pytest по требованию -> стадия runtime собирает боевой образ
# из venv и каталога src.
# вход: requirements.txt, каталоги src, tests и certs; адреса зеркал приходят
# через build-args.
# выход: образ с точкой входа `python -m src.cli`, командой по умолчанию serve
# и каталогом базы /opt/sofi-mail/data.
# настройки демон читает из окружения через src/config.py, файл .env
# подключается при запуске (см. docker-compose.yml).
#
# всё, что загружается из сети, вынесено в build-args: в закрытом контуре
# базовый образ и пакеты приходят из Sonatype Nexus.
#
# перед развёртыванием подставить адреса своего Nexus:
#   docker build \
#     --build-arg BASE_IMAGE=nexus.company.ru:8083/python:3.12-slim-bookworm \
#     --build-arg PIP_INDEX_URL=https://nexus.company.ru/repository/pypi-proxy/simple \
#     -t sofi-mail:1.0 .
#
# сборка двухэтапная: компиляторы участвуют только в сборке зависимостей.
# боевой образ хранит переписку должностных лиц, и набор инструментов в нём
# ограничен тем, что нужно демону для работы.

ARG BASE_IMAGE=python:3.12-slim-bookworm

# --- Сборка зависимостей ----------------------------------------------------
FROM ${BASE_IMAGE} AS builder

# пустые значения направляют сборку в pypi.org и deb.debian.org: этот вариант
# работает на машине с доступом в интернет. в контуре компании сюда
# подставляются адреса Nexus
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""
ARG DEBIAN_MIRROR=""

# переменные гасят проверку версии pip, кеш колёс и предупреждение о запуске
# от root: слои образа от них растут, а пользы в сборке они не дают
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# заголовки libxml2 и libxslt нужны сборке lxml (зависимость exchangelib)
# из исходников: прокси Nexus отдаёт то, что успел закешировать, и готового
# колеса под нужную версию там может не оказаться
RUN set -eu; \
    if [ -n "$DEBIAN_MIRROR" ]; then \
        sed -i "s|https\?://deb.debian.org|${DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i "s|https\?://deb.debian.org|${DEBIAN_MIRROR}|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev; \
    rm -rf /var/lib/apt/lists/*

# зависимости ставятся в отдельный venv: в боевой слой копируется один каталог,
# содержимое которого перечислено в requirements.txt
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt копируется до исходников: правка кода оставляет слой
# с зависимостями в кеше сборки
COPY requirements.txt ./
RUN set -eu; \
    PIP_ARGS=""; \
    if [ -n "$PIP_INDEX_URL" ]; then PIP_ARGS="--index-url $PIP_INDEX_URL"; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then PIP_ARGS="$PIP_ARGS --trusted-host $PIP_TRUSTED_HOST"; fi; \
    pip install $PIP_ARGS --upgrade pip; \
    pip install $PIP_ARGS -r requirements.txt

# --- Тесты ------------------------------------------------------------------
# стадия запускается по требованию командой `docker build --target test .`
# и в боевой образ не попадает. проверяется тот набор зависимостей, который
# уедет в боевой слой; сетевые вызовы подменены заглушками tests/conftest.py,
# и прогон обходится без Exchange и без модели.
# pytest ставится здесь, а не на стадии builder: боевой слой копирует /opt/venv
# целиком, и тестовый фреймворк уехал бы в образ с перепиской
FROM builder AS test

ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

COPY requirements-dev.txt ./
RUN set -eu; \
    PIP_ARGS=""; \
    if [ -n "$PIP_INDEX_URL" ]; then PIP_ARGS="--index-url $PIP_INDEX_URL"; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then PIP_ARGS="$PIP_ARGS --trusted-host $PIP_TRUSTED_HOST"; fi; \
    pip install $PIP_ARGS -r requirements-dev.txt

WORKDIR /opt/sofi-mail
COPY src/ ./src/
COPY tests/ ./tests/
RUN python -m pytest -q

# --- Боевой образ -----------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

# идентификаторы пользователя и группы задаются на сборке: каталог базы
# принадлежит этому пользователю, и при монтировании каталога с хоста
# значения должны совпадать с владельцем каталога на хосте
ARG APP_UID=1000
ARG APP_GID=1000

LABEL org.opencontainers.image.title="sofi-mail" \
      org.opencontainers.image.description="Чат с локальной LLM через электронную почту" \
      org.opencontainers.image.source="https://github.com/"

# PYTHONUNBUFFERED отдаёт лог в docker logs без задержки буфера,
# PYTHONDONTWRITEBYTECODE оставляет каталоги кода без файлов .pyc,
# HOME=/tmp даёт библиотекам доступный на запись домашний каталог
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

# процесс демона работает под непривилегированным пользователем: он читает
# письма из сети и пишет в базу персональные данные.
#
# это пользователь операционной системы внутри контейнера, к домену
# и к Exchange отношения не имеющий: на EWS демон входит под доменной учётной
# записью из MAIL_LOGIN. здесь задаётся владелец процесса и каталога с базой,
# совпадение имени с адресом ящика случайное
RUN groupadd --gid "${APP_GID}" sofi \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
            --home-dir /tmp --shell /usr/sbin/nologin sofi

COPY --from=builder /opt/venv /opt/venv

WORKDIR /opt/sofi-mail

# код принадлежит root, пользователю демона он доступен на чтение:
# скомпрометированный процесс собственный код не перезаписывает
COPY --chown=root:root src/ ./src/
# корневые сертификаты внутреннего УЦ для MAIL_CA_FILE и LLM_CA_FILE.
# каталог допускает пустое состояние: сертификаты монтируются при запуске
COPY --chown=root:root certs/ ./certs/

# каталог базы. права 0700 и 0600 демон выставляет сам в storage.connect,
# а владельцем каталога должен быть непривилегированный пользователь: первая
# запись под другим владельцем закончится отказом в доступе.
# пустой каталог из образа передаёт владельца и права именованному тому
# при его создании
RUN install -d -m 700 -o "${APP_UID}" -g "${APP_GID}" /opt/sofi-mail/data

USER sofi

# ENTRYPOINT задаёт точку входа cli, CMD — команду по умолчанию, поэтому
# записи `docker run sofi-mail check` и `... serve -w 4` работают без ключа
# --entrypoint.
# сигнал SIGTERM обрабатывает сам демон (pipeline.run_forever), init-обёртка
# в образе не нужна: команда docker stop приводит к штатной остановке
ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["serve"]
