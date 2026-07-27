"""TLS-контекст для почтовых соединений.

Отдельный модуль, потому что контекст нужен трём клиентам сразу (IMAP, SMTP,
EWS), а правила у всех одни: проверять сертификат по умолчанию и уметь доверять
внутреннему удостоверяющему центру.
"""

import logging
import ssl
from typing import Optional

from src.config import MAIL_CA_FILE, MAIL_TLS_VERIFY

log = logging.getLogger(__name__)


def build_ssl_context() -> Optional[ssl.SSLContext]:
    """Контекст для imaplib/smtplib или None, если сойдёт дефолт stdlib.

    Дефолт stdlib здесь — ловушка: при `context=None` и `imaplib`, и `smtplib`
    берут `ssl._create_stdlib_context()` с `verify_mode=CERT_NONE` и
    `check_hostname=False`, то есть молча не проверяют сертификат вообще.
    Поэтому явный контекст — не перестраховка, а исправление небезопасного
    поведения по умолчанию.
    """
    if not MAIL_TLS_VERIFY:
        # Осознанный отказ от проверки: самоподписанный сертификат Exchange,
        # когда корневого сертификата внутреннего УЦ ещё нет на руках
        log.warning("проверка TLS-сертификата отключена (MAIL_TLS_VERIFY=false)")
        return ssl._create_unverified_context()

    return ssl.create_default_context(cafile=MAIL_CA_FILE or None)
