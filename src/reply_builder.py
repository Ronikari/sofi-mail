# сборка MIME-сообщения с ответом модели.
# порядок: текст ответа и данные треда -> метка [Sofi] в начале тела ->
# заголовки From/To/Subject/Date -> генерация Message-ID -> заголовки треда
# In-Reply-To и References -> заголовки разговора Thread-Topic и Thread-Index ->
# заголовки подавления автоответов -> тело с подписью и цитатой.
# время в шапке цитаты пишется в часовом поясе процесса (переменная TZ):
# без неё процесс работает в UTC и пользователь видит время со сдвигом.
# значение задано в docker-compose.yml (TZ=Europe/Moscow).
# вход: адрес получателя, тема входящего письма, текст ответа модели, название
# сессии, Message-ID входящего письма, его цепочка References, заголовки
# разговора Exchange, имя и дата отправителя входящего письма и его текст
# для цитаты.
# выход: объект EmailMessage; ews_client.py отдаёт его Exchange байтами.
# MAIL_ADDRESS и MAIL_DISPLAY_NAME импортируются из config.py,
# REPLY_MARKER и LOOP_HEADER — из email_parser.py.
# вызывается из ews_client.py, метод EWSTransport.send_reply.

import base64
import email.utils
import logging
import os
import time
import uuid
from email.message import EmailMessage
from typing import List, Optional

from src.config import (
    MAIL_ADDRESS,
    MAIL_DISPLAY_NAME,
)
from src.email_parser import LOOP_HEADER, REPLY_MARKER

log = logging.getLogger(__name__)

# предел длины цепочки References. часть почтовых серверов обрезает более
# длинную цепочку по своим правилам, и тред у получателя распадается.
# по общепринятой практике сохраняются корень цепочки и ближайшие предки
MAX_REFERENCES = 20

# длина заголовочного блока Thread-Index в байтах: признак версии (1 байт),
# усечённая метка времени FILETIME (5 байт), идентификатор разговора (16 байт).
# формат описан в MS-OXOMSG, раздел ConversationIndex
_THREAD_INDEX_HEAD = 22

# длина блока ответа, который дописывается к заголовочному на каждом шаге
# переписки: разница времени (4 байта) и счётчик со случайными битами (1 байт)
_THREAD_INDEX_STEP = 5

# сдвиг от эпохи FILETIME (1601-01-01) до эпохи unix в интервалах по 100 нс
_FILETIME_EPOCH = 116444736000000000


# выход: текущее время в формате FILETIME — интервалы по 100 нс от 1601-01-01
def _filetime() -> int:
    """Отдаёт текущее время в единицах, которыми размечен Thread-Index."""
    return int(time.time() * 10_000_000) + _FILETIME_EPOCH


# вход: текст ответа модели.
# выход: тот же текст с меткой REPLY_MARKER в первой строке.
# метка стоит первой строкой каждого исходящего письма и служит признаком
# реплики модели при разборе переписки: и человеком, и внешним парсером,
# и самой моделью, когда клиент пользователя цитирует прежние письма треда.
# функция идемпотентна: повторный вызов второй метки не добавляет
def mark_answer(body: str) -> str:
    """Ставит метку [Sofi] в начало ответа модели."""
    text = body.lstrip()

    # метка уже стоит: текст пришёл из pipeline, который помечает ответ
    # до записи в таблицу messages
    if text.startswith(REPLY_MARKER):
        return text

    return f"{REPLY_MARKER} {text}"


# вход: тема входящего письма и её же значение из заголовка Thread-Topic,
# если клиент его прислал.
# выход: тема разговора без префиксов Re: и Fwd:.
# Exchange сверяет это значение при отнесении письма к разговору, поэтому
# оно совпадает у всех писем треда
def thread_topic(subject: str, incoming_topic: str = "") -> str:
    """Отдаёт тему разговора для заголовка Thread-Topic."""
    from src.email_parser import normalize_subject

    # значение из входящего письма имеет приоритет: его задал клиент,
    # начавший разговор, и Exchange сверяет ответ именно с ним
    return incoming_topic.strip() or normalize_subject(subject)


# вход: значение заголовка Thread-Index входящего письма в base64; пустая
# строка допустима.
# выход: значение Thread-Index для ответа в base64.
# ответ получает тот же заголовочный блок, что и входящее письмо, плюс блок
# своего шага: по совпадению заголовочного блока Outlook и OWA относят письмо
# к разговору входящего.
# при пустом либо неразборчивом значении собирается новый корень разговора
def next_thread_index(parent: str = "") -> str:
    """Строит Thread-Index ответа как продолжение разговора входящего письма."""
    now = _filetime()

    raw = b""
    if parent:
        try:
            raw = base64.b64decode(parent, validate=True)
        # ветка испорченного заголовка: разговор начинается заново, письмо
        # остаётся в треде по In-Reply-To и References
        except Exception:
            log.debug("Thread-Index входящего письма не разбирается: %r", parent[:64])
            raw = b""

    # значение короче заголовочного блока разговором не является
    if len(raw) < _THREAD_INDEX_HEAD:
        # корень разговора: признак версии, метка времени и новый идентификатор.
        # метка времени занимает 5 байт из середины FILETIME — младшие 16 бит
        # и старшие 8 отбрасываются
        head = b"\x01" + ((now >> 16) & 0xFF_FFFF_FFFF).to_bytes(5, "big") + uuid.uuid4().bytes
        return base64.b64encode(head).decode("ascii")

    # метка времени корня восстанавливается обратным сдвигом
    started = int.from_bytes(raw[1:6], "big") << 16
    delta = max(0, now - started)

    # разница времени укладывается в 31 бит после сдвига: младший сдвиг даёт
    # разрешение около 26 мс, старший включается на разговорах длиннее ~6 суток
    if delta < (1 << 49):
        code, value = 0, delta >> 18
    else:
        code, value = 1, delta >> 23

    # порядковый номер шага занимает младшие 4 бита последнего байта, старшие
    # 4 отведены под случайные: так задан формат
    step = (len(raw) - _THREAD_INDEX_HEAD) // _THREAD_INDEX_STEP + 1
    tail = ((code << 31) | (value & 0x7FFF_FFFF)).to_bytes(4, "big")
    tail += bytes([(os.urandom(1)[0] & 0xF0) | (step & 0x0F)])

    return base64.b64encode(raw + tail).decode("ascii")


# названия месяцев в родительном падеже: Outlook пишет дату строкой
# «9 сентября 2026 г. 10:00». strftime("%B") сюда не годится — он берёт
# название из локали процесса, а локаль контейнера задана как C
_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


# вход: значение заголовка Date входящего письма в форме RFC 5322.
# выход: та же дата в формате строки «Отправлено:» русского Outlook.
# дата приводится к часовому поясу машины: заголовок Date исходящего письма
# ставится тем же поясом (formatdate(localtime=True)), и обе даты письма
# читаются в одной шкале.
# разбор неудался — строка возвращается как есть: показать дату в чужом
# формате лучше, чем потерять её
def format_sent_date(raw_date: str) -> str:
    """Переводит дату письма из формата RFC 5322 в формат Outlook."""
    if not raw_date:
        return ""

    try:
        parsed = email.utils.parsedate_to_datetime(raw_date)
    # ветка нечитаемого заголовка Date: ValueError на битом значении,
    # TypeError на None из parsedate_tz у отдельных версий
    except (ValueError, TypeError):
        log.debug("заголовок Date не разобран, в цитату уходит исходная строка")
        return raw_date

    # письмо без часового пояса в заголовке считается отправленным по местному
    # времени: astimezone() наивную дату трактует именно так
    local = parsed.astimezone()

    month = _MONTHS_GENITIVE[local.month - 1]
    return f"{local.day} {month} {local.year} г. {local:%H:%M}"


# вход: имя, отображаемое в поле «От:» цитаты, и его адрес.
# выход: строка вида «Имя <адрес>»; при пустом имени — один адрес.
# formataddr сюда не годится: он кодирует нелатинское имя по RFC 2047 для
# заголовка, а эта строка идёт в тело письма как обычный текст — получатель
# увидел бы в цитате буквальный «=?utf-8?b?...?=» вместо имени
def _display_address(name: str, address: str) -> str:
    """Собирает пару «имя, адрес» строкой для тела письма без кодирования RFC 2047."""
    return f"{name} <{address}>" if name else address


# вход: имя и адрес отправителя входящего письма, дата и тема этого письма.
# выход: шапка цитаты в формате Outlook «От:/Отправлено:/Кому:/Тема:».
# этот же набор меток разбирает email_parser._HEADER_LABEL, поэтому шапка
# опознаётся как цитата и у нас самих, если письмо вернётся во входящие.
# строка "Кому:" ставится адресом ящика модели: это и есть адрес, на который
# пользователь отправил цитируемое письмо
def build_quote_header(sender_name: str, sender: str, sent_date: str, subject: str) -> str:
    """Строит шапку цитаты входящего письма в формате Outlook."""
    from_line = _display_address(sender_name, sender)
    to_line = _display_address(MAIL_DISPLAY_NAME, MAIL_ADDRESS)

    lines = [f"От: {from_line}"]
    # дата приходит из заголовка Date входящего письма и пустой не бывает
    # у настоящей почты; строка опускается только у синтетических тестов
    if sent_date:
        lines.append(f"Отправлено: {format_sent_date(sent_date)}")
    lines.append(f"Кому: {to_line}")
    lines.append(f"Тема: {subject}")
    return "\n".join(lines)


# выход: две строки — разделитель подписи и строка с REPLY_MARKER.
# пара «метка в первой строке тела, метка в подписи» задаёт границы нашего
# ответа внутри цитаты: по ним email_parser.strip_own_replies вырезает его
# из письма пользователя, не трогая его собственный текст.
# состав строки ограничен маркером и названием сессии: MAIL_DISPLAY_NAME
# пользователь уже видит в поле «От», а идентификатор модели относится
# к внутренней настройке сервиса
def build_footer(session_title: str) -> str:
    """Собирает подпись письма с техническим маркером и названием сессии."""
    return f"-- \n{REPLY_MARKER} · сессия «{session_title}»"


# вход: to_address — адрес пользователя; subject — тема входящего письма;
# body — текст ответа модели; in_reply_to и references — заголовки треда
# из входящего письма, при первом письме сессии равны None; thread_index
# и incoming_topic — заголовки разговора Exchange из входящего письма.
# выход: EmailMessage с заполненным Message-ID; значение заголовка читает
# ews_client.py и передаёт в storage.add_message.
# побочные эффекты отсутствуют, сеть не используется
def build_reply(
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
) -> EmailMessage:
    """Собирает ответное письмо с заголовками, склеивающими тред у получателя."""
    message = EmailMessage()

    # formataddr даёт форму «Имя <адрес>» с корректным кодированием имени
    message["From"] = email.utils.formataddr((MAIL_DISPLAY_NAME, MAIL_ADDRESS))
    message["To"] = to_address

    # префикс Re: добавляется к теме, которая его ещё не содержит; почтовые
    # клиенты накладывают такие префиксы каскадом
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    # formatdate(localtime=True) даёт дату в формате RFC 5322 с часовым поясом машины
    message["Date"] = email.utils.formatdate(localtime=True)

    # значение Message-ID создаётся до передачи письма Exchange: строка с ним
    # пишется в таблицу messages, и заголовок In-Reply-To входящего ответа
    # сопоставляется с этой строкой в storage.find_session_by_message_ids.
    # домен для идентификатора берётся из части MAIL_ADDRESS после @
    message["Message-ID"] = email.utils.make_msgid(domain=MAIL_ADDRESS.split("@")[-1] or None)

    # ветка первого письма сессии заголовки треда пропускает: предка нет
    if in_reply_to:
        # заполняются оба заголовка: Gmail собирает тред по References,
        # Outlook по In-Reply-To
        message["In-Reply-To"] = in_reply_to

        # цепочка предков дополняется идентификатором входящего письма
        chain = list(references or [])
        if in_reply_to not in chain:
            chain.append(in_reply_to)

        # срез оставляет корень треда и MAX_REFERENCES-1 ближайших предков,
        # середина цепочки отбрасывается
        if len(chain) > MAX_REFERENCES:
            chain = chain[:1] + chain[-(MAX_REFERENCES - 1):]

        # заголовок хранит идентификаторы через пробел, формат задан RFC 5322
        message["References"] = " ".join(chain)

    # заголовки разговора Exchange: Outlook и OWA относят письмо к разговору
    # по ним. без Thread-Index ответ ложится в папку отдельным письмом,
    # и пользователь видит его как новую переписку
    message["Thread-Topic"] = thread_topic(subject, incoming_topic)
    message["Thread-Index"] = next_thread_index(thread_index)

    # заголовки RFC 3834 останавливают автоответчик на стороне получателя:
    # без них пара автоответчиков образует бесконечный обмен письмами
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"

    # собственная метка: письмо с ней, пришедшее во входящие, отбрасывается
    # в email_parser.automated_reason
    message[LOOP_HEADER] = "1"

    # тело письма: метка [Sofi], текст ответа, пустая строка, подпись с маркером
    content = f"{mark_answer(body)}\n\n{build_footer(session_title)}\n"

    # цитата ставится строго после подписи с маркером: strip_own_replies
    # и strip_quoted режут тело по REPLY_MARKER раньше, чем доходят до неё,
    # и следующая реплика сессии её не подхватывает.
    # без цитаты письмо несёт заголовки треда, но выглядит как новое
    # сообщение: адресат не видит в нём ссылки на своё конкретное письмо
    if quoted_body:
        quote_header = build_quote_header(sender_name, to_address, sent_date, subject)
        content += f"\n{quote_header}\n\n{quoted_body}\n"

    message.set_content(content, subtype="plain", charset="utf-8")
    return message
