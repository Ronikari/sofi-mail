# приём и отправка писем через Exchange Web Services.
# порядок: подключение к ящику (autodiscover либо явный сервер) -> чтение
# непрочитанных писем из папки EWS_FOLDER -> отдача сырого MIME вызывающему
# модулю -> отправка ответа -> простановка признака прочитанности.
# вход: настройки ящика и аутентификации из config.py; текст ответа модели
# и заголовки треда от pipeline.py.
# выход: пары (объект письма exchangelib, разобранный MIME) и Message-ID
# отправленного письма.
# MIME ответа собирает reply_builder.build_reply, адреса и темы для лога
# маскирует redact.py.
# класс EWSTransport создаёт transport.get_transport, работает с ним pipeline.py.
#
# EWS — протокол, которым работает Outlook, и он включён в Exchange постоянно:
# служба MSExchangeIMAP4 в Exchange 2013 и новее запускается отдельно,
# а Basic-аутентификацию на почтовых протоколах закрывают в пользу
# NTLM, Kerberos и OAuth2.
#
# письма читаются и отправляются сырым MIME (поле mime_content). благодаря
# этому email_parser.py и reply_builder.py работают с email.message.Message
# и прогоняются на .eml-файлах без Exchange, а Message-ID, созданный
# в reply_builder, доходит до сервера неизменным. чтение идентификатора
# из папки «Отправленные» после отправки оставило бы сопоставление тредов
# на фоллбэке по теме письма.
#
# exchangelib импортируется внутри функций: он тянет requests, lxml и pyspnego,
# и команды, работающие без почты, эту загрузку пропускают

import email
import logging
import os
import threading
from email.message import Message
from typing import Any, List, Optional, Sequence, Tuple

from src import redact
from src.config import (
    EWS_ACCESS_TYPE,
    EWS_AUTH,
    EWS_CLIENT_ID,
    EWS_CLIENT_SECRET,
    EWS_ENDPOINT,
    EWS_FOLDER,
    EWS_SAVE_SENT,
    EWS_SERVER,
    EWS_TENANT_ID,
    EWS_TIMEOUT_SEC,
    MAIL_ADDRESS,
    MAIL_CA_FILE,
    MAIL_LOGIN,
    MAIL_PASSWORD,
    MAIL_TLS_VERIFY,
    WORKERS,
)

log = logging.getLogger(__name__)

# поля письма, запрашиваемые у сервера: сырой MIME для разбора и служебные
# для простановки is_read. вызов .only() ограничивает выборку — запрос без него
# тянет десятки свойств на каждое письмо
_FETCH_FIELDS = ("id", "changekey", "mime_content", "datetime_received", "subject")

# режимы, работающие по билету Kerberos из кеша; пароль в них не участвует
_PASSWORDLESS_AUTH = ("gssapi", "sspi")


# выход: константа exchangelib для значения EWS_AUTH; None оставляет выбор
# библиотеке.
# поднимает ValueError при неизвестном значении
def _auth_type() -> Optional[str]:
    """Переводит значение EWS_AUTH в константу аутентификации exchangelib."""
    from exchangelib import BASIC, CBA, DIGEST, GSSAPI, NTLM, OAUTH2, SSPI

    known = {
        "basic": BASIC,
        "ntlm": NTLM,
        "gssapi": GSSAPI,
        "sspi": SSPI,
        "digest": DIGEST,
        "cba": CBA,
        "oauth2": OAUTH2,
    }

    # пустое значение включает определение режима средствами exchangelib
    if not EWS_AUTH:
        return None

    # опечатка в .env обнаруживается здесь, до первого запроса к серверу
    if EWS_AUTH not in known:
        raise ValueError(f"EWS_AUTH={EWS_AUTH!r}: допустимы {', '.join(sorted(known))}")

    return known[EWS_AUTH]


# побочный эффект: подмена класса http-адаптера exchangelib, запись переменной
# REQUESTS_CA_BUNDLE в окружение процесса и установка BaseProtocol.TIMEOUT.
# exchangelib выполняет запросы библиотекой requests, поэтому ssl-контекст
# из llm_backend.py здесь не применяется: отказ от проверки задаётся
# http-адаптером, корневой сертификат внутреннего УЦ — переменной окружения
def _apply_tls_policy() -> None:
    """Настраивает проверку tls-сертификата и таймаут запросов к Exchange."""
    from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

    # BaseProtocol.TIMEOUT задаёт предел ожидания для каждого запроса requests.
    # без него запрос к недоступному серверу висит до таймаута сокета
    # операционной системы, поток обработки письма занят всё это время,
    # а заявка остаётся в статусе processing
    BaseProtocol.TIMEOUT = EWS_TIMEOUT_SEC

    # MAIL_TLS_VERIFY=false заменяет адаптер на вариант без проверки сертификата
    if not MAIL_TLS_VERIFY:
        log.warning("проверка TLS-сертификата EWS отключена (MAIL_TLS_VERIFY=false)")
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    # setdefault сохраняет значение, заданное снаружи процесса
    elif MAIL_CA_FILE:
        os.environ.setdefault("REQUESTS_CA_BUNDLE", MAIL_CA_FILE)


# выход: объект учётных данных exchangelib; None для режимов по билету Kerberos
def _credentials():
    """Собирает учётные данные под режим аутентификации из EWS_AUTH."""
    from exchangelib import Credentials, Identity, OAuth2Credentials, OAuth2LegacyCredentials

    if EWS_AUTH in _PASSWORDLESS_AUTH:
        return None  # Kerberos/SSPI берут билет из кеша

    # режимы basic, ntlm и digest принимают логин с паролем
    if EWS_AUTH != "oauth2":
        return Credentials(username=MAIL_LOGIN, password=MAIL_PASSWORD)

    # oauth2 с паролем работает по потоку ROPC (обмен логина и пароля на токен):
    # приложение действует от имени пользователя, тип доступа остаётся delegate
    if MAIL_PASSWORD:
        log.debug("EWS: OAuth2 от имени пользователя %s", MAIL_LOGIN)
        return OAuth2LegacyCredentials(
            username=MAIL_LOGIN,
            password=MAIL_PASSWORD,
            client_id=EWS_CLIENT_ID,
            client_secret=EWS_CLIENT_SECRET,
            tenant_id=EWS_TENANT_ID or None,
        )

    # oauth2 без пароля работает по потоку client credentials: токен выдаётся
    # регистрации приложения. это режим службы, пароль пользователя нигде
    # не хранится, и поток ROPC выше закрыт политикой многих тенантов.
    # Identity задаёт ящик заголовком impersonation: контекст ящика
    # в таком токене отсутствует
    log.debug("EWS: OAuth2 от имени приложения, ящик %s", MAIL_ADDRESS)
    return OAuth2Credentials(
        client_id=EWS_CLIENT_ID,
        client_secret=EWS_CLIENT_SECRET,
        tenant_id=EWS_TENANT_ID or None,
        identity=Identity(primary_smtp_address=MAIL_ADDRESS),
    )


# выход: объект Account exchangelib, подключённый к ящику MAIL_ADDRESS.
# побочные эффекты: настройка политики tls и сетевые запросы к Exchange
def _build_account():
    """Подключается к ящику через явный сервер либо через autodiscover."""
    from exchangelib import Account, Configuration, DELEGATE, IMPERSONATION

    _apply_tls_policy()

    credentials = _credentials()

    # delegate означает права, выданные учётной записи на ящик; impersonation —
    # работу служебной учётной записи от имени ящика
    access_type = IMPERSONATION if EWS_ACCESS_TYPE == "impersonation" else DELEGATE

    # ветка явного адреса: autodiscover в закрытых сетях недоступен
    if EWS_SERVER or EWS_ENDPOINT:
        config = Configuration(
            credentials=credentials,
            server=EWS_SERVER or None,
            service_endpoint=EWS_ENDPOINT or None,
            auth_type=_auth_type(),
            # размер пула соединений: WORKERS потоков обработки плюс один
            # на опрос папки. значение по умолчанию в exchangelib меньше,
            # и письма ждут освобождения соединения на уровне http
            max_connections=WORKERS + 1,
        )
        log.debug("EWS: явный сервер %s", EWS_ENDPOINT or EWS_SERVER)
        return Account(
            primary_smtp_address=MAIL_ADDRESS,
            config=config,
            autodiscover=False,
            access_type=access_type,
        )

    # ветка autodiscover: сервер определяется по домену адреса ящика
    log.debug("EWS: autodiscover по адресу %s", MAIL_ADDRESS)
    return Account(
        primary_smtp_address=MAIL_ADDRESS,
        credentials=credentials,
        autodiscover=True,
        access_type=access_type,
    )


# реализация протокола MailTransport из transport.py поверх exchangelib
class EWSTransport:
    def __init__(self) -> None:
        # соединение открывается лениво при первом обращении к свойству account
        self._account = None

        # exchangelib потокобезопасен на уровне запросов, но само создание
        # Account (autodiscover, определение версии сервера) — нет
        self._lock = threading.Lock()

    # --- соединение ---------------------------------------------------------

    # выход: объект Account; первое обращение открывает соединение.
    # замок нужен параллельным воркерам: без него два потока создали бы
    # два подключения к одному ящику
    @property
    def account(self):
        with self._lock:
            if self._account is None:
                self._account = _build_account()
            return self._account

    # побочный эффект: сброс текущего соединения и открытие нового.
    # вызывается из цикла демона pipeline.run_forever после разрыва
    def reconnect(self) -> None:
        """Пересоздаёт подключение к ящику."""
        with self._lock:
            self._account = None
        # обращение к свойству открывает соединение здесь: ошибка подключения
        # поднимается из reconnect, до возврата управления в цикл опроса
        self.account  # noqa: B018

    # побочный эффект: закрытие http-сессии exchangelib
    def close(self) -> None:
        """Закрывает подключение к ящику."""
        with self._lock:
            if self._account is not None:
                try:
                    self._account.protocol.close()
                # сбой закрытия на дальнейшую работу не влияет: ссылка
                # на соединение снимается строкой ниже
                except Exception:
                    log.debug("EWS закрыт с ошибкой", exc_info=True)
                self._account = None

    # выход: объект папки exchangelib для чтения писем
    def _folder(self):
        """Отдаёт папку приёма, заданную значением EWS_FOLDER."""
        folder = self.account.inbox

        # значение inbox оставляет папку «Входящие»; прочие значения задают
        # путь вложенной папки
        if EWS_FOLDER and EWS_FOLDER.lower() != "inbox":
            # оператор / у exchangelib спускается на один уровень вложенности
            for part in EWS_FOLDER.split("/"):
                folder = folder / part

        return folder

    # --- приём -------------------------------------------------------------

    # выход: список пар (объект письма exchangelib, разобранный MIME),
    # упорядоченный по времени получения.
    # признак is_read здесь не выставляется: письмо считается обработанным
    # после успешной отправки ответа, отметку ставит pipeline вызовом mark_seen.
    # побочный эффект: сетевые запросы к Exchange
    def fetch_unseen(self) -> List[Tuple[Any, Message]]:
        """Читает непрочитанные письма из папки приёма."""
        # порядок по datetime_received сохраняет последовательность реплик треда
        items = (
            self._folder()
            .filter(is_read=False)
            .only(*_FETCH_FIELDS)
            .order_by("datetime_received")
        )

        result: List[Tuple[Any, Message]] = []
        for item in items:
            # пустое поле mime_content приходит у календарных приглашений
            # и прочих элементов папки, не являющихся письмами
            if not getattr(item, "mime_content", None):
                log.warning(
                    "письмо без mime_content пропущено: %s",
                    redact.subject(getattr(item, "subject", "") or ""),
                )
                continue

            # message_from_bytes разбирает сырой MIME в email.message.Message,
            # дальше с ним работает email_parser.parse_email
            result.append((item, email.message_from_bytes(item.mime_content)))

        return result

    # вход: дескриптор письма из fetch_unseen.
    # побочный эффект: запись признака is_read на сервере
    def mark_seen(self, handle: Any) -> None:
        """Помечает письмо прочитанным."""
        handle.is_read = True

        # update_fields ограничивает запрос одним полем: вызов без него
        # отправляет на сервер весь объект письма
        handle.save(update_fields=["is_read"])

    # вход: дескрипторы писем одного прохода.
    # побочный эффект: один запрос UpdateItem на всю пачку.
    # проход после простоя демона приносит десятки писем, и отметка по одному
    # дала бы столько же последовательных обращений к Exchange
    def mark_seen_bulk(self, handles: Sequence[Any]) -> None:
        """Помечает пачку писем прочитанными за один запрос."""
        if not handles:
            return

        for item in handles:
            item.is_read = True

        # bulk_update принимает пары (письмо, список полей) и складывает их
        # в один запрос
        self.account.bulk_update([(item, ["is_read"]) for item in handles])

    # вход: Message-ID письма из таблицы processed.
    # выход: число писем, у которых снят признак прочитанности; 0 означает,
    # что письма в папке уже нет.
    # побочный эффект: запись признака is_read на сервере.
    # вызывается из pipeline.retry_failed по команде cli retry
    def unsee_by_message_id(self, message_id: str) -> int:
        """Возвращает письмо в очередь обработки, снимая признак прочитанности."""
        count = 0
        for item in self._folder().filter(message_id=message_id).only("id", "changekey", "is_read"):
            item.is_read = False
            item.save(update_fields=["is_read"])
            count += 1
        return count

    # --- отправка ----------------------------------------------------------

    # вход: адрес получателя, тема входящего письма, текст ответа модели,
    # название сессии, заголовки треда и заголовки разговора Exchange, имя,
    # дата, текст и разметка входящего письма для цитаты.
    # выход: Message-ID отправленного письма; pipeline пишет его в таблицу messages.
    # побочный эффект: отправка письма через Exchange.
    #
    # ответ собирается тем же способом, каким собирает его Outlook по кнопке
    # «Ответить»: письмо составляется на нашей стороне и уходит сырым MIME
    # через операцию CreateItem. ответом на письмо пользователя его делают
    # заголовки треда, собранные в reply_builder — In-Reply-To, References,
    # Thread-Topic и Thread-Index, — и цитата вопроса в теле, блок
    # «От:/Отправлено:/Кому:/Тема:».
    # серверная сборка ответа (ReplyToItem) для этого не годится: она
    # не принимает ни Message-ID, назначенный здесь, ни собственные заголовки
    # X-Sofi и Auto-Submitted, на которых держится защита от почтовой петли
    def send_reply(
        self,
        to_address: str,
        subject: str,
        body: str,
        session_title: str,
        in_reply_to: Optional[str] = None,
        references: Optional[List[str]] = None,
        thread_index: str = "",
        incoming_topic: str = "",
        sender_name: str = "",
        quoted_body: str = "",
        sent_date: str = "",
        quoted_html: str = "",
    ) -> str:
        """Отправляет ответ пользователю и возвращает Message-ID письма."""
        from exchangelib import Message as EWSMessage

        from src.reply_builder import build_reply

        # письмо собирается целиком в reply_builder: заголовки треда и разговора,
        # метка [Sofi], подпись с маркером, цитата входящего письма и Message-ID
        # задаются там
        mime = build_reply(
            to_address, subject, body, session_title, in_reply_to, references,
            thread_index, incoming_topic, sender_name, quoted_body, sent_date,
            quoted_html,
        )

        # идентификатор читается до отправки: он нужен вызывающему коду
        # независимо от исхода отправки
        message_id = mime["Message-ID"]

        # as_bytes отдаёт письмо сырым MIME, Exchange принимает его без разбора
        item = EWSMessage(account=self.account, mime_content=mime.as_bytes())

        # ящик модели один на всех пользователей сервиса, поэтому его папка
        # «Отправленные» собирает ответы всем: владелец прав на ящик читает
        # переписку каждого пользователя. история ответов хранится в таблице
        # messages, у пользователя ответ остаётся в его почте.
        # значение EWS_SAVE_SENT=true включает копию там, где её требуют
        # правила хранения переписки
        if EWS_SAVE_SENT:
            item.send_and_save()
        else:
            item.send()

        log.info("отправлено (EWS) -> %s", redact.email_addr(to_address))
        return message_id

    # --- диагностика -------------------------------------------------------

    # выход: строка с адресом точки входа, адресом ящика и счётчиками писем.
    # побочный эффект: открытие соединения и запрос к Exchange
    def describe(self) -> str:
        """Отдаёт строку о состоянии ящика для команды check."""
        folder = self._folder()
        return (
            f"EWS {self.account.protocol.service_endpoint}, ящик {MAIL_ADDRESS}, "
            f"папка {folder.name}: {folder.total_count} писем, {folder.unread_count} непрочитанных"
        )
