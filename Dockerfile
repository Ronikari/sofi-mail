# Образ демона sofi-mail.
#
# Всё, что тянется из сети, вынесено в build-args: в закрытом контуре базовый
# образ и пакеты приходят из Sonatype Nexus, а не с docker.io и pypi.org.
#
# ИЗМЕНИТЬ ССЫЛКУ ПЕРЕД РАЗВЕРТЫВАНИЕМ
#   docker build \
#     --build-arg BASE_IMAGE=nexus.company.ru:8083/python:3.12-slim-bookworm \
#     --build-arg PIP_INDEX_URL=https://nexus.company.ru/repository/pypi-proxy/simple \
#     -t sofi-mail:1.0 .
#
# Сборка двухэтапная: компиляторы нужны только затем, чтобы собрать зависимости,
# и в боевом образе их быть не должно — лишний компилятор рядом с перепиской
# должностных лиц это инструмент для того, кто до этого образа доберётся.

ARG BASE_IMAGE=python:3.12-slim-bookworm

# --- Сборка зависимостей ----------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ARG CI_PACKGE_REGISTRY_USER=""
ARG CI_PACKGE_REGISTRY_PASSWORD=""

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY ["./CA_ia_fpa_nexus.crt", "/usr/local/share/ca-certificates/"]
COPY ./sources.list /etc/apt/sources.list
COPY pip.conf /etc/ 

RUN sed -i "s/user/${CI_PACKGE_REGISTRY_USER}/g; s/password/${CI_PACKGE_REGISTRY_PASSWORD}/g" /etc/pip.conf
RUN mkdir -p /etc/ssl/certs/

# lxml (зависимость exchangelib) обычно приезжает готовым колесом, но прокси
# Nexus отдаёт то, что успел закешировать, и на sdist сборка не должна падать
RUN set -eu; \
    cat /usr/local/share/ca-certificates/CA_ia_fpa_nexus.crt >> /etc/ssl/certs/ca-certificates.crt; \
    printf 'machine asdu-fpa-nexus.cdu.so\nlogin %s\npassword %s\n' \
        "${CI_PACKGE_REGISTRY_USER}" "${CI_PACKGE_REGISTRY_PASSWORD}" \
        > /etc/apt/auth.conf.d/nexus.conf; \
    chmod 600 /etc/apt/auth.conf.d/nexus.conf; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev; \
    rm -rf /var/lib/apt/lists/*

# venv, а не установка в системный python: в боевой слой уезжает один каталог,
# и в нём ровно то, что перечислено в requirements.txt
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN set -eu; \
    pip install --upgrade "pip==26.2.1"; \
    pip install -r requirements.txt

# --- Боевой образ -----------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

# UID/GID задаются на сборке: каталог базы принадлежит этому пользователю,
# и при bind-mount он должен совпадать с владельцем каталога на хосте
ARG APP_UID=1000
ARG APP_GID=1000

LABEL org.opencontainers.image.title="sofi-mail" \
      org.opencontainers.image.description="Чат с локальной LLM через электронную почту" \
      org.opencontainers.image.source="https://asdu-fpa-gitlab.cdu.so/ai-projects/sofi-mail"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

# Демон не должен ходить root'ом: он читает письма из сети и складывает
# в базу ПДн — ровно та задача, где лишние права не нужны
RUN groupadd --gid "${APP_GID}" sofi \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
            --home-dir /tmp --shell /usr/sbin/nologin sofi

COPY --from=builder /opt/venv /opt/venv

WORKDIR /opt/sofi-mail

# Код принадлежит root и пользователю демона доступен только на чтение:
# скомпрометированный процесс не переписывает собственный код
COPY --chown=root:root src/ ./src/
# Корневые сертификаты внутреннего УЦ (MAIL_CA_FILE / LLM_CA_FILE). Каталог
# может быть пустым — тогда сертификаты монтируются при запуске
COPY --chown=root:root certs/ ./certs/

# Каталог базы. Права демон ужесточает и сам (0700/0600 в storage.connect),
# но владельцем должен быть непривилегированный пользователь, иначе первая же
# запись упрётся в отказ. Пустой каталог из образа наследует и владельца,
# и права при создании именованного тома
RUN install -d -m 700 -o "${APP_UID}" -g "${APP_GID}" /opt/sofi-mail/data

USER sofi

# ENTRYPOINT — точка входа CLI, CMD — команда по умолчанию. Поэтому
# `docker run sofi-mail check` и `... serve -w 4` работают без --entrypoint.
# SIGTERM обрабатывается самим демоном (pipeline.run_forever), init-обёртка
# не нужна: docker stop приводит к штатной остановке
ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["serve"]
