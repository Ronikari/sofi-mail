# сборка MIME-ответа модели: текст, заголовки треда и цитата двумя частями (text/plain и text/html).
# время в шапке цитаты пишется в часовом поясе процесса (TZ=Europe/Moscow в docker-compose.yml) — иначе UTC вместо локального.

import base64
import email.utils
import html
import logging
import os
import re
import time
import uuid
from email.message import EmailMessage
from typing import List, Optional, Tuple

from src.config import (
    MAIL_ADDRESS,
    MAIL_DISPLAY_NAME,
)
from src.email_parser import LOOP_HEADER, REPLY_MARKER

log = logging.getLogger(__name__)

# предел длины цепочки References: часть серверов обрезает её, и тред у получателя распадается.
# сохраняются корень цепочки и ближайшие предки.
MAX_REFERENCES = 20

# длина заголовочного блока Thread-Index в байтах: версия (1), усечённая метка времени FILETIME (5), id разговора (16).
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


# метка REPLY_MARKER в начале письма — признак реплики модели при разборе переписки (человеком, парсером, моделью при цитировании).
# функция идемпотентна: повторный вызов не добавляет вторую метку.
def mark_answer(body: str) -> str:
    """Ставит метку [Sofi] в начало ответа модели."""
    text = body.lstrip()

    # метка уже стоит: текст пришёл из pipeline, который помечает ответ
    # до записи в таблицу messages
    if text.startswith(REPLY_MARKER):
        return text

    return f"{REPLY_MARKER} {text}"


# тема разговора без Re:/Fwd: — Exchange сверяет это значение при отнесении письма к разговору, оно должно совпадать у всех писем треда.
def thread_topic(subject: str, incoming_topic: str = "") -> str:
    """Отдаёт тему разговора для заголовка Thread-Topic."""
    from src.email_parser import normalize_subject

    # значение из входящего письма имеет приоритет: его задал клиент,
    # начавший разговор, и Exchange сверяет ответ именно с ним
    return incoming_topic.strip() or normalize_subject(subject)


# ответ получает заголовочный блок входящего письма плюс блок своего шага — по совпадению блока Outlook и OWA относят письмо к разговору.
# при пустом либо неразборчивом значении собирается новый корень разговора.
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
        # корень разговора: версия, метка времени (5 байт из середины FILETIME —
        # младшие 16 и старшие 8 бит отброшены) и новый id.
        head = b"\x01" + ((now >> 16) & 0xFF_FFFF_FFFF).to_bytes(5, "big") + uuid.uuid4().bytes
        return base64.b64encode(head).decode("ascii")

    # now усекается той же 40-битной маской, что и started, иначе разница получает
    # паразитный сдвиг ~2**56 (228 лет) — раньше code почти всегда выходил 1 вместо 0.
    started = int.from_bytes(raw[1:6], "big") << 16
    now_truncated = ((now >> 16) & 0xFF_FFFF_FFFF) << 16
    delta = max(0, now_truncated - started)

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


# названия месяцев в родительном падеже для формата даты Outlook.
# strftime("%B") не годится: берёт название из локали процесса, а локаль контейнера — C.
_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


# дата приводится к часовому поясу машины — тем же поясом ставится заголовок Date исходящего письма, обе даты читаются в одной шкале.
# разбор неудался — строка возвращается как есть: показать дату в чужом формате лучше, чем потерять её.
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


# formataddr сюда не годится: кодирует нелатинское имя по RFC 2047 для заголовка,
# а эта строка идёт в тело письма как текст — получатель увидел бы «=?utf-8?b?...?=» вместо имени.
def _display_address(name: str, address: str) -> str:
    """Собирает пару «имя, адрес» строкой для тела письма без кодирования RFC 2047."""
    return f"{name} <{address}>" if name else address


# те же метки разбирает email_parser._HEADER_LABEL — шапка опознаётся как цитата, если письмо вернётся во входящие.
# "Кому:" — адрес ящика модели: письмо, на который пользователь и отправил цитируемое.
def build_quote_header(sender_name: str, sender: str, sent_date: str, subject: str) -> str:
    """Строит шапку цитаты входящего письма в формате Outlook."""
    return "\n".join(
        f"{label}: {value}"
        for label, value in quote_header_fields(sender_name, sender, sent_date, subject)
    )


# состав шапки задан одним местом: text-часть склеивает пары двоеточием, html-часть ставит метку полужирной.
# разойтись они не могут — иначе выглядело бы как два разных письма в одном.
def quote_header_fields(
    sender_name: str, sender: str, sent_date: str, subject: str
) -> List[Tuple[str, str]]:
    """Отдаёт поля шапки цитаты парами «метка, значение»."""
    fields = [("От", _display_address(sender_name, sender))]

    # дата приходит из заголовка Date входящего письма и пустой не бывает
    # у настоящей почты; строка опускается только у синтетических тестов
    if sent_date:
        fields.append(("Отправлено", format_sent_date(sent_date)))

    fields.append(("Кому", _display_address(MAIL_DISPLAY_NAME, MAIL_ADDRESS)))
    fields.append(("Тема", subject))
    return fields


# Liberation Serif с запасом Times New Roman (метрически совпадает) — стоит не в каждой системе.
# начертание задаётся только нашему тексту: цитата пользователя несёт свои стили и переопределяет его.
_HTML_FONT = "font-family:'Liberation Serif','Times New Roman',Georgia,serif; font-size:12pt; color:#000000"
_HTML_QUOTE_RULE = "border-top:1px solid #E1E1E1; margin-top:12pt; padding-top:6pt"

# регулярные выражения, а не разбор дерева: нужно вырезать <body> байт в байт,
# любой разбор с пересборкой нарушил бы точное совпадение.
_BODY_OPEN = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE = re.compile(r"</body\s*>", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*("[^"]*"|'[^']*')""", re.IGNORECASE)


# экранирование обязательно: текст приходит от модели и пользователя,
# угловая скобка иначе становится тегом.
def _html_text(text: str) -> str:
    """Переводит текст письма в html с сохранением переводов строк."""
    return html.escape(text).replace("\n", "<br>\n")


# полный документ вставить внутрь другого нельзя — вложенные <html>/<head> клиенты отбрасывают вместе с содержимым.
# правила <style> переносятся вперёд, иначе теряется оформление по классам; разметка без <body> (так шлёт Gmail) возвращается целиком.
def html_quote_fragment(markup: str) -> Tuple[str, str]:
    """Готовит разметку письма пользователя к вставке в цитату ответа."""
    if not markup:
        return "", ""

    opening = _BODY_OPEN.search(markup)
    if not opening:
        return "", markup

    # атрибут style тега <body> переносится на обёртку цитаты: Outlook держит
    # в нём перенос слов, и без него длинные строки цитаты ведут себя иначе
    attr = _STYLE_ATTR.search(opening.group(0))
    style = attr.group(1).strip("\"'") if attr else ""

    closing = _BODY_CLOSE.search(markup, opening.end())
    inner = markup[opening.end():closing.start()] if closing else markup[opening.end():]

    # правила из шапки документа: они стоят до <body> и в inner не попадают
    rules = "".join(_STYLE_BLOCK.findall(markup[:opening.start()]))

    return style, rules + inner


# повторяет текстовую часть по составу и порядку блоков: клиент показывает одну из двух на выбор,
# расхождение означало бы, что пользователи разных клиентов читают разные письма.
def build_html_body(
    marked_answer: str,
    footer: str,
    header_fields: List[Tuple[str, str]],
    quoted_body: str,
    quoted_html: str = "",
) -> str:
    """Собирает html-часть письма: ответ, подпись и цитату входящего письма."""
    parts = [
        f"<div>{_html_text(marked_answer)}</div>",
        "<div>&nbsp;</div>",
        f"<div>{_html_text(footer)}</div>",
    ]

    if header_fields:
        # метка полужирная, значение обычным начертанием — так шапку цитаты
        # рисует Outlook
        rows = "<br>\n".join(
            f"<b>{html.escape(label)}:</b> {html.escape(value)}"
            for label, value in header_fields
        )
        parts.append(f'<div style="{_HTML_QUOTE_RULE}">{rows}</div>')
        parts.append("<div>&nbsp;</div>")

        # разметка пользователя вставляется как есть — цитата выглядит ровно тем письмом со своим начертанием, таблицами, картинками.
        # письмо без html-части цитируется текстом, экранированным как обычно.
        if quoted_html:
            style, fragment = html_quote_fragment(quoted_html)
            wrapper = f' style="{html.escape(style, quote=True)}"' if style else ""
            parts.append(f"<div{wrapper}>{fragment}</div>")
        else:
            parts.append(f"<div>{_html_text(quoted_body)}</div>")

    body = "\n".join(parts)
    return f'<html><body style="{_HTML_FONT}">\n{body}\n</body></html>'


# пара меток (в теле и в подписи) задаёт границы ответа внутри цитаты — по ним email_parser.strip_own_replies вырезает его, не трогая текст пользователя.
# состав строки ограничен маркером и названием сессии: MAIL_DISPLAY_NAME уже виден в поле «От».
def build_footer(session_title: str) -> str:
    """Собирает подпись письма с техническим маркером и названием сессии."""
    return f"-- \n{REPLY_MARKER} · сессия «{session_title}»"


# EmailMessage с заполненным Message-ID — его читает ews_client.py и передаёт в storage.add_message.
# побочных эффектов нет, сеть не используется.
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
    quoted_html: str = "",
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

    # Message-ID создаётся до передачи Exchange: пишется в таблицу messages,
    # In-Reply-To входящего ответа сопоставляется с ней в storage.find_session_by_message_ids.
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

    # Outlook и OWA относят письмо к разговору по этим заголовкам.
    # без Thread-Index ответ ложится отдельным письмом — пользователь видит новую переписку.
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
    marked_answer = mark_answer(body)
    footer = build_footer(session_title)
    content = f"{marked_answer}\n\n{footer}\n"

    # цитата ставится строго после подписи: strip_own_replies/strip_quoted режут тело по REPLY_MARKER раньше и не подхватят её в следующей реплике.
    # без цитаты письмо выглядит новым сообщением без ссылки на письмо адресата.
    header_fields: List[Tuple[str, str]] = []
    if quoted_body:
        header_fields = quote_header_fields(sender_name, to_address, sent_date, subject)
        content += f"\n{build_quote_header(sender_name, to_address, sent_date, subject)}\n\n{quoted_body}\n"

    # text/plain стоит первой и остаётся ведущей — её же читает email_parser.extract_body при возврате письма во входящие.
    # html-часть нужна ради вида в треде: шапка цитаты идёт полужирными метками под линией, как у Outlook, а не сплошным текстом.
    message.set_content(content, subtype="plain", charset="utf-8")
    message.add_alternative(
        build_html_body(marked_answer, footer, header_fields, quoted_body, quoted_html),
        subtype="html",
        charset="utf-8",
    )
    return message
