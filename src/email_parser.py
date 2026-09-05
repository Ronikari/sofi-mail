"""Разбор входящего письма: заголовки, тело, отсечение цитаты.

Здесь только чистые функции над `email.message.Message` — без сети и без БД,
чтобы всю логику можно было прогнать на сохранённых .eml-фикстурах.
"""

import email.utils
import hashlib
import logging
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from typing import List, Optional, Sequence, Tuple

from src.attachments import Attachment

log = logging.getLogger(__name__)

# Технический маркер в подписи наших писем. Формат исходящего письма — наш,
# поэтому резать цитату по собственному маркеру надёжнее любой эвристики.
# Не зависит от MAIL_DISPLAY_NAME, иначе переименование сломало бы разбор
# ответов на уже отправленные письма.
REPLY_MARKER = "[Sofi]"

# наш заголовок в исходящих: если письмо с ним пришло обратно, где-то замкнулась петля
LOOP_HEADER = "X-Sofi"

NO_SUBJECT_TITLE = "без темы"

# если после отсечения цитаты осталось меньше — считаем, что эвристика промахнулась
MIN_BODY_CHARS = 2

# порядок перебора кодировок, когда заявленный charset соврал или отсутствует;
# у российских клиентов cp1251/koi8-r встречаются до сих пор, а latin-1 не падает
# никогда и потому замыкает цепочку
_CHARSET_FALLBACKS = ("utf-8", "cp1251", "koi8-r", "latin-1")

# префиксы ответа/пересылки: снимаются повторно, пока хоть один совпадает.
# "пер" — пересылка в русском Outlook и OWA
_SUBJECT_PREFIX = re.compile(
    r"^\s*(re|re\s*\[\d+\]|re\d+|fwd|fw|ответ|отв|пер|пересылаемое\s+сообщение)\s*:\s*",
    re.IGNORECASE,
)

# Outlook, настроенный на формат RTF, кладёт тело письма в winmail.dat вместо
# text/plain и text/html. Разбирать TNEF мы не берёмся, но отличить этот случай
# от «пользователь прислал пустое письмо» обязаны: подсказки нужны разные
_TNEF_TYPES = ("application/ms-tnef", "application/vnd.ms-tnef")

# начало цитаты предыдущего письма; первое совпадение = конец полезного текста
_QUOTE_PATTERNS = [
    re.compile(r"^\s*>"),
    re.compile(
        r"^\s*-{2,}\s*(Original Message|Исходное сообщение|Пересылаемое сообщение|Forwarded message)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*_{5,}\s*$"),  # разделитель Outlook
    # Самый общий признак атрибуции: строка заканчивается адресом в угловых
    # скобках и двоеточием. Так подписывают цитату Gmail (обе локали), Яндекс
    # и Mail.ru — одна регулярка вместо трёх клиентских.
    re.compile(r"^\s*.{0,300}<[^<>@\s]+@[^<>\s]+>\s*:\s*$"),
    # Gmail EN: "On Fri, Jul 25, 2026 at 7:12 PM Name wrote:"
    re.compile(
        r"^\s*(On|В)\s+.{0,300}?(wrote|написал\(а\)|написала|написал|пишет)\s*:\s*$",
        re.IGNORECASE,
    ),
    # Gmail RU начинает с дня недели: "вт, 25 июл. 2026 г. в 18:56, Имя ...:"
    re.compile(r"^\s*(пн|вт|ср|чт|пт|сб|вс)\s*,\s*\d{1,2}\s+\S+.{0,300}:\s*$", re.IGNORECASE),
    # Яндекс/Mail.ru: "25.07.2026, 19:12, "Имя" <a@b>:" / "25 июля 2026, 19:12 ... написал:"
    re.compile(r"^\s*\d{1,2}[.\s]\S+[.\s]\s*\d{4}.{0,300}(написал|wrote|>)\s*:?\s*$", re.IGNORECASE),
]
# Шапки «От:/Отправлено:/Кому:/Тема:» здесь намеренно нет: одиночная строка
# такого вида — это и «Кому: отделу продаж» в письме пользователя, по которому
# резать нельзя. Шапку целиком опознаёт find_header_block ниже.

# стандартный разделитель подписи по RFC 3676 — строка ровно "-- "
_SIGNATURE_DELIMITER = re.compile(r"^\s*--\s?$")

# Заголовки письма, из которых Outlook, OWA и Exchange собирают шапку цитаты
# («От:/Отправлено:/Кому:/Тема:» и англоязычные эквиваленты). Метка ловится
# и в середине строки: html_to_text склеивает текст пользователя с шапкой,
# когда клиент разделил их <span>, а не <br>.
_HEADER_LABEL = re.compile(
    r"(?:^|(?<=[\s>\"'»)\].,;!?]))"
    r"(?:От|From|Отправитель|Sender|Кому|To|Копия|Cc|Скрытая копия|Bcc|"
    r"Тема|Subject|Отправлено|Sent|Дата|Date|Reply-To|Ответить)"
    r"\s*:(?=\s|$)",
    re.IGNORECASE,
)

# сколько пустых строк допускается внутри шапки: html_to_text ставит перевод
# строки на каждый <p>/<div>, и поля шапки расходятся на отдельные абзацы
_HEADER_BLOCK_GAP = 2


def _split_lines(text: str) -> List[str]:
    """Текст в строки с приведением переводов строк к \n."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _label_positions(line: str) -> List[int]:
    """Смещения меток заголовков в строке."""
    return [m.start() for m in _HEADER_LABEL.finditer(line)]


def _next_nonblank(lines: Sequence[str], index: int) -> Optional[str]:
    """Ближайшая непустая строка после `index` — не дальше, чем через пробел."""
    for j in range(index + 1, min(index + 1 + _HEADER_BLOCK_GAP, len(lines))):
        if lines[j].strip():
            return lines[j]
    return None


def _block_start(lines: Sequence[str], index: int) -> Optional[int]:
    """Смещение, с которого в строке начинается шапка цитаты, или None.

    Одиночной метки мало: строка «Кому: отделу продаж» в письме пользователя —
    его собственный текст, а не цитата. Шапку опознаём по паре меток: рядом
    в одной строке либо в соседней. У Outlook их всегда минимум три
    (От/Отправлено/Кому/Тема), поэтому признак срабатывает на любой из них
    и не зависит от порядка полей.
    """
    positions = _label_positions(lines[index])
    if not positions:
        return None
    if len(positions) >= 2:
        return positions[0]
    following = _next_nonblank(lines, index)
    if following is not None and _label_positions(following):
        return positions[0]
    return None


def find_header_block(lines: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Первая шапка цитаты как (номер строки, смещение в строке)."""
    for index in range(len(lines)):
        offset = _block_start(lines, index)
        if offset is not None:
            return index, offset
    return None


def strip_header_blocks(text: str) -> str:
    """Вырезать шапки «От:/Отправлено:/Кому:/Тема:», оставив остальной текст.

    Последняя линия обороны для случаев, когда отсечь цитату целиком не вышло
    (например, пользователь дописал ответ под цитатой). Для модели такая шапка —
    граница письма: всё, что до неё, читается как чужая переписка, и контекст
    сессии рассыпается, хотя история в базе цела.
    """
    lines = _split_lines(text)
    kept: List[str] = []
    index = 0
    while index < len(lines):
        offset = _block_start(lines, index)
        if offset is None:
            kept.append(lines[index])
            index += 1
            continue

        head = lines[index][:offset].rstrip()
        if head:
            kept.append(head)
        # съедаем шапку целиком: строки с метками и пустые строки внутри неё
        index += 1
        while index < len(lines):
            if _label_positions(lines[index]):
                index += 1
                continue
            following = _next_nonblank(lines, index)
            if not lines[index].strip() and following is not None and _label_positions(following):
                index += 1
                continue
            break

    # на месте вырезанной шапки остаются пустые строки, которые её обрамляли
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


@dataclass
class IncomingEmail:
    """Всё, что нужно пайплайну от письма, в готовом к использованию виде."""

    message_id: str
    sender: str  # адрес в нижнем регистре
    sender_name: str
    subject: str  # тема как есть, декодированная
    title: str  # нормализованная тема = название сессии
    body: str  # текст без цитаты предыдущего письма
    in_reply_to: Optional[str]
    references: List[str] = field(default_factory=list)
    date: str = ""
    body_raw: str = ""  # тело до очистки — видно, где промахнулась эвристика цитат
    is_tnef: bool = False  # тело в winmail.dat: пустой body объясняется форматом письма
    attachments: List["Attachment"] = field(default_factory=list)

    @property
    def ancestor_ids(self) -> List[str]:
        """Message-ID предков от ближайшего к дальнему — порядок поиска сессии."""
        ids = [self.in_reply_to] if self.in_reply_to else []
        ids += [ref for ref in reversed(self.references) if ref != self.in_reply_to]
        return ids


def decode_mime_header(raw: Optional[str]) -> str:
    """Заголовок в человеческий вид: RFC 2047 (=?utf-8?B?...?=) -> unicode."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:  # битая кодировка не должна ронять обработку письма
        log.warning("не удалось декодировать заголовок: %r", raw[:100])
        return raw.strip()


def normalize_subject(subject: str) -> str:
    """Тема без Re:/Fwd:/Ответ: — она же название сессии."""
    title = subject.strip()
    while True:
        stripped = _SUBJECT_PREFIX.sub("", title, count=1)
        if stripped == title:
            break
        title = stripped
    return " ".join(title.split()) or NO_SUBJECT_TITLE


def parse_message_ids(raw: Optional[str]) -> List[str]:
    """Разбор References/In-Reply-To в список <id> в порядке появления."""
    if not raw:
        return []
    return re.findall(r"<[^<>\s]+>", raw)


class _HTMLTextExtractor(HTMLParser):
    """Снятие тегов с обрывом на первом блоке цитаты.

    В HTML-письмах цитата всегда завёрнута в опознаваемый контейнер
    (blockquote у всех, gmail_quote у Gmail, divRplyFwdMsg у Outlook), поэтому
    в HTML её видно точнее, чем в plain text по эвристикам.
    """

    _BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}
    _SKIP = {"style", "script", "head"}
    _QUOTE_IDS = {"divrplyfwdmsg", "appendonlast", "stopspelling", "mail-editor-reference-message-container"}
    _QUOTE_CLASSES = ("gmail_quote", "yahoo_quoted", "moz-cite-prefix", "protonmail_quote")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0
        self._done = False

    def _is_quote_start(self, tag: str, attrs) -> bool:
        if tag == "blockquote":
            return True
        values = {k.lower(): (v or "").lower() for k, v in attrs}
        if values.get("id", "") in self._QUOTE_IDS:
            return True
        klass = values.get("class", "")
        return any(marker in klass for marker in self._QUOTE_CLASSES)

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if self._is_quote_start(tag, attrs):
            self._done = True
            return
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._done:
            return
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._done or self._skip_depth:
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = raw.replace("\xa0", " ")
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(lines)


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def _decode_part(part: Message) -> str:
    """Байты части письма в текст.

    Заявленному charset нельзя верить: старые клиенты пишут "utf-8", отправляя
    windows-1251, а иногда charset отсутствует вовсе и кириллица гибнет как
    us-ascii. Поэтому перебираем кодировки строго, без errors="replace", —
    первая, которая не упала, и есть настоящая.
    """
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""

    declared = part.get_content_charset()
    for charset in (declared, *_CHARSET_FALLBACKS):
        if not charset:
            continue
        try:
            return payload.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def extract_body(msg: Message) -> str:
    """Текст письма: text/plain приоритетнее, text/html — запасной вариант."""
    plain: Optional[str] = None
    html: Optional[str] = None

    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif ctype == "text/html" and html is None:
            html = _decode_part(part)

    if plain and plain.strip():
        return plain
    if html:
        return html_to_text(html)
    return ""


def has_tnef(msg: Message) -> bool:
    """Тело письма приехало в winmail.dat (формат RTF в Outlook)?

    Такое письмо выглядит как пустое: ни text/plain, ни text/html в нём нет.
    Пользователю нужно сказать про формат письма, а не про «нет текста вопроса».
    """
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type().lower() in _TNEF_TYPES:
            return True
        filename = (part.get_filename() or "").lower()
        if filename == "winmail.dat":
            return True
    return False


def extract_attachments(msg: Message) -> List[Attachment]:
    """Файлы, приложенные пользователем к письму.

    Отсекается ровно то, что вложением не является по смыслу, а не по формату:

    - части с Content-ID — картинки из тела письма и подписи (логотип компании,
      скриншот, вставленный прямо в текст). Их у делового письма бывает
      по десятку, к вопросу они отношения не имеют, а в Open WebUI уехали бы
      наравне с документами. Судим по Content-ID, а не по одному лишь
      `Content-Disposition: inline`: Outlook помечает встроенную картинку
      то так, то иначе, а ссылка из тела письма на неё есть всегда. Картинка,
      приложенная человеком осознанно, Content-ID не имеет и до разбора
      доедет — там ей ответят, что распознавания текста в сервисе нет;
    - `winmail.dat` — контейнер TNEF, про который у пайплайна свой ответ
      (см. `has_tnef`), а не «формат не поддерживается»;
    - части без имени файла: у вложения оно есть всегда, а безымянные части —
      это тело письма и его alternative-варианты.

    Порядок сохраняется: пользователь ссылается на файлы в том порядке,
    в каком приложил их к письму («по первому документу — вопрос такой»).
    """
    found: List[Attachment] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        filename = decode_mime_header(part.get_filename())
        if not filename or filename.lower() == "winmail.dat":
            continue
        disposition = str(part.get("Content-Disposition", "")).lower()
        embedded = part.get("Content-ID") and (
            "inline" in disposition or part.get_content_maintype() == "image"
        )
        if embedded:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # битая base64-часть не должна ронять письмо целиком
            log.warning("вложение %s не декодируется, пропускаю", filename)
            continue
        if not payload:
            continue
        found.append(Attachment(filename, part.get_content_type(), payload))
    return found


def strip_quoted(text: str) -> str:
    """Отсечь цитату предыдущего письма и подпись.

    Без этого промпт растёт с каждым ответом на весь предыдущий тред, а модель
    начинает отвечать на собственную прошлую реплику вместо нового вопроса.
    """
    lines = _split_lines(text)
    cut = len(lines)
    tail = ""  # хвост строки cut: текст пользователя, к которому приклеена шапка

    for i, line in enumerate(lines):
        # 1. собственный маркер — самый надёжный признак начала нашего письма
        if REPLY_MARKER in line:
            cut = i
            break
        # 2. разделитель подписи по RFC
        if _SIGNATURE_DELIMITER.match(line):
            cut = i
            break
        # 3. эвристики почтовых клиентов; "On ... wrote:" часто переносится
        #    на вторую строку, поэтому проверяем и склейку с соседней
        candidates = [line]
        if i + 1 < len(lines) and not line.rstrip().endswith(":"):
            candidates.append(f"{line.rstrip()} {lines[i + 1].strip()}")
        if any(pattern.search(c) for pattern in _QUOTE_PATTERNS for c in candidates):
            cut = i
            break

    # Шапка цитаты ищется отдельно от эвристик выше и побеждает при равенстве:
    # она находит начало цитаты там, где построчные признаки промахиваются —
    # когда шапка начинается не с «От:», а с «Тема:», и когда клиент приклеил
    # её прямо к тексту пользователя без перевода строки.
    block = find_header_block(lines)
    if block is not None and block[0] <= cut:
        cut, offset = block
        tail = lines[cut][:offset].rstrip()

    kept = lines[:cut]
    if tail:
        kept.append(tail)
    return "\n".join(kept).strip()


def automated_reason(msg: Message, own_address: str) -> Optional[str]:
    """Почему на письмо нельзя отвечать (иначе получится почтовая петля).

    None — письмо живое, можно обрабатывать.
    """
    sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    if sender and own_address and sender == own_address.lower():
        return "письмо от самого себя"

    if msg.get(LOOP_HEADER):
        return "письмо помечено как наше собственное — замкнулась петля"

    if msg.get_content_type() == "multipart/report":
        return "отчёт о доставке (DSN)"

    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"

    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "auto_reply", "junk"):
        return f"Precedence: {precedence}"

    for header in ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Autorespond"):
        if msg.get(header):
            return f"служебный заголовок {header}"

    local_part = sender.split("@")[0]
    if re.fullmatch(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce\S*", local_part):
        return f"адрес-автоответчик: {sender}"

    return None


def synthetic_message_id(msg: Message) -> str:
    """Заменитель Message-ID для писем, где его нет.

    Message-ID обязателен по RFC, но встречаются письма без него; журнал
    обработки завязан на этот ключ, поэтому строим стабильный суррогат
    из тех полей, которые точно есть.
    """
    seed = "|".join(
        [msg.get("From", ""), msg.get("Subject", ""), msg.get("Date", ""), msg.get("To", "")]
    )
    return f"<synthetic-{hashlib.sha256(seed.encode('utf-8', 'replace')).hexdigest()[:32]}@local>"


def parse_email(msg: Message) -> IncomingEmail:
    """Письмо -> структура, с которой работает пайплайн."""
    sender_name, sender = email.utils.parseaddr(decode_mime_header(msg.get("From")))
    subject = decode_mime_header(msg.get("Subject"))
    message_id = (msg.get("Message-ID") or "").strip() or synthetic_message_id(msg)
    in_reply_to_ids = parse_message_ids(msg.get("In-Reply-To"))

    raw_body = extract_body(msg)
    body = strip_quoted(raw_body)
    # Откат на неочищенное тело: слишком жадная регулярка молча отдала бы модели
    # пустой промпт, и это выглядело бы как «модель тупит», а не как баг разбора.
    # Но отдать тело совсем как есть нельзя: так в промпт и в историю сессии
    # уезжают шапки «От:/Отправлено:/Кому:/Тема:», а вместе с ними — вся прежняя
    # переписка треда. Реплика раздувается до размеров всего треда и на следующем
    # письме не влезает в окно модели, обнуляя контекст сессии целиком.
    if len(body) < MIN_BODY_CHARS and raw_body.strip():
        log.warning("отсечение цитаты дало пустой текст (%s), беру тело без шапок", message_id)
        body = strip_header_blocks(raw_body) or raw_body.strip()

    return IncomingEmail(
        message_id=message_id,
        sender=sender.lower(),
        sender_name=sender_name,
        subject=subject,
        title=normalize_subject(subject),
        body=body,
        in_reply_to=in_reply_to_ids[0] if in_reply_to_ids else None,
        references=parse_message_ids(msg.get("References")),
        date=(msg.get("Date") or "").strip(),
        body_raw=raw_body,
        is_tnef=not body and has_tnef(msg),
        attachments=extract_attachments(msg),
    )
