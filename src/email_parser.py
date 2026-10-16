# Разбор входящего письма: заголовки -> тело -> отсечение цитаты -> вложения -> IncomingEmail.
# Сеть и база здесь не используются: разбор гоняется на .eml-файлах в тестах.

import email.utils
import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from html.parser import HTMLParser
from typing import List, Optional, Sequence, Tuple
from urllib.parse import unquote

from src.attachments import Attachment

log = logging.getLogger(__name__)

# Маркер в подписи исходящих писем — по нему точно определяется граница цитаты в формате писем проекта.
# Задан литералом, не выводится из MAIL_DISPLAY_NAME: переименование ящика не должно ломать границу в уже отправленных письмах.
REPLY_MARKER = "[Sofi]"

# собственный заголовок исходящих писем. письмо с ним во входящих означает
# замкнувшуюся почтовую петлю
LOOP_HEADER = "X-Sofi"

# название сессии для письма с пустой темой
NO_SUBJECT_TITLE = "без темы"

# длина тела в символах, ниже которой отсечение цитаты считается промахом
MIN_BODY_CHARS = 2

# Порядок перебора кодировок при неверном/отсутствующем charset — cp1251 и koi8-r ещё встречаются у российских клиентов.
# latin-1 замыкает цепочку: декодирование в неё успешно на любых байтах.
_CHARSET_FALLBACKS = ("utf-8", "cp1251", "koi8-r", "latin-1")

# префиксы ответа и пересылки; снимаются повторно, пока совпадает хоть один.
# "пер" ставит русский Outlook и OWA при пересылке
_SUBJECT_PREFIX = re.compile(
    r"^\s*(re|re\s*\[\d+\]|re\d+|fwd|fw|ответ|отв|пер|пересылаемое\s+сообщение)\s*:\s*",
    re.IGNORECASE,
)

# TNEF (winmail.dat) — контейнер Outlook в режиме RTF, тело внутри него, text/plain и text/html отсутствуют.
# Разбор контейнера не реализован; признак нужен, чтобы отличить такое письмо от письма с пустым телом — подсказки разные.
_TNEF_TYPES = ("application/ms-tnef", "application/vnd.ms-tnef")

# признаки начала цитаты предыдущего письма; первое совпадение обозначает
# конец текста пользователя
_QUOTE_PATTERNS = [
    re.compile(r"^\s*>"),
    re.compile(
        r"^\s*-{2,}\s*(Original Message|Исходное сообщение|Пересылаемое сообщение|Forwarded message)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*_{5,}\s*$"),  # разделитель Outlook
    # Общий признак строки атрибуции: адрес в угловых скобках и двоеточие в конце строки.
    # Покрывает цитаты Gmail (обе локали), Яндекс и Mail.ru одним выражением.
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
# Шапка «От:/Отправлено:/Кому:/Тема:» не входит в этот список: такая строка одна встречается и в тексте пользователя («Кому: отделу продаж»).
# Шапку целиком опознаёт find_header_block ниже.

# разделитель подписи по RFC 3676: строка ровно из двух дефисов и пробела
_SIGNATURE_DELIMITER = re.compile(r"^\s*--\s?$")

# Метки полей шапки цитаты (Outlook/OWA/Exchange).
# Выражение допускает метку в середине строки: html_to_text склеивает текст пользователя с шапкой при разделении тегом <span>.
_HEADER_LABEL = re.compile(
    r"(?:^|(?<=[\s>\"'»)\].,;!?]))"
    r"(?:От|From|Отправитель|Sender|Кому|To|Копия|Cc|Скрытая копия|Bcc|"
    r"Тема|Subject|Отправлено|Sent|Дата|Date|Reply-To|Ответить)"
    r"\s*:(?=\s|$)",
    re.IGNORECASE,
)

# сколько пустых строк допускается внутри шапки: html_to_text ставит перевод
# строки на каждый тег <p> и <div>, и поля шапки расходятся по абзацам
_HEADER_BLOCK_GAP = 2

# префиксы темы, которыми почтовые клиенты помечают пересылку.
# "пер" ставит русский Outlook и OWA, "fwd" и "fw" — англоязычные клиенты
_FORWARD_SUBJECT = re.compile(r"^\s*(fwd|fw|пер|пересылаемое\s+сообщение)\s*:", re.IGNORECASE)

# Разделители, которыми клиенты открывают тело пересланного письма.
# divRplyFwdMsg — из html-разметки Outlook, доживает до текста, когда контейнер цитаты по нему не опознался.
_FORWARD_BODY = re.compile(
    r"(Пересылаемое\s+сообщение|Forwarded\s+message|Начало\s+пересылаемого\s+сообщения|divRplyFwdMsg)",
    re.IGNORECASE,
)


# выход: список строк; последовательности \r\n и \r приводятся к \n,
# иначе нумерация строк расходится с исходным текстом
def _split_lines(text: str) -> List[str]:
    """Делит текст на строки с приведением переводов строк к одному виду."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


# выход: смещения меток заголовков внутри строки, пустой список при их отсутствии
def _label_positions(line: str) -> List[int]:
    """Находит в строке смещения меток полей шапки цитаты."""
    return [m.start() for m in _HEADER_LABEL.finditer(line)]


# вход: строки и индекс текущей. выход: ближайшая непустая строка в пределах _HEADER_BLOCK_GAP вперёд, None если нет.
def _next_nonblank(lines: Sequence[str], index: int) -> Optional[str]:
    """Ищет ближайшую непустую строку после указанной."""
    # верхняя граница диапазона ограничена и длиной списка, и шириной разрыва
    for j in range(index + 1, min(index + 1 + _HEADER_BLOCK_GAP, len(lines))):
        if lines[j].strip():
            return lines[j]
    return None


# вход: список строк и индекс проверяемой строки.
# выход: смещение начала шапки внутри строки, None при отсутствии шапки
def _block_start(lines: Sequence[str], index: int) -> Optional[int]:
    """Определяет, начинается ли в строке шапка цитаты, и с какого смещения."""
    positions = _label_positions(lines[index])

    # строка без единой метки шапкой не является
    if not positions:
        return None

    # две метки в одной строке служат достаточным признаком: одиночная строка
    # «Кому: отделу продаж» принадлежит тексту пользователя
    if len(positions) >= 2:
        return positions[0]

    # Одиночная метка засчитывается при метке в соседней строке: Outlook ставит минимум три поля шапки.
    # Признак срабатывает на любом из них и не зависит от порядка.
    following = _next_nonblank(lines, index)
    if following is not None and _label_positions(following):
        return positions[0]

    return None


# выход: пара (номер строки, смещение в строке) для первой найденной шапки,
# None при её отсутствии
def find_header_block(lines: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Находит первую шапку цитаты в списке строк."""
    for index in range(len(lines)):
        offset = _block_start(lines, index)
        if offset is not None:
            return index, offset
    return None


# Строка, которой открывается наш прошлый ответ внутри цитаты — метка стоит первой в теле каждого исходящего письма.
# Префикс допускает пробелы и знаки цитирования ">", которые дописывает клиент.
_OWN_REPLY_START = re.compile(rf"^[\s>]*{re.escape(REPLY_MARKER)}\s")

# строка подписи исходящего письма: та же метка и разделитель " · ".
# подпись замыкает наш ответ, поэтому служит его нижней границей
_OWN_REPLY_END = re.compile(rf"^[\s>]*{re.escape(REPLY_MARKER)}\s*·")


# Убирает из текста наши прошлые ответы: их границы (метка [Sofi] и подпись) заданы форматом письма точно, без эвристик.
# Без вырезки ответ модели дублировался бы в реплике пользователя — в messages он уже лежит своей строкой.
def strip_own_replies(text: str) -> str:
    """Вырезает из текста прежние ответы модели вместе с их подписью."""
    lines = _split_lines(text)
    kept: List[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        # одиночная подпись без тела перед ней: снимается вместе с разделителем
        # RFC 3676, который клиент оставил строкой выше
        if _OWN_REPLY_END.match(line):
            if kept and _SIGNATURE_DELIMITER.match(kept[-1]):
                kept.pop()
            index += 1
            continue

        # строка пользователя переносится в результат
        if not _OWN_REPLY_START.match(line):
            kept.append(line)
            index += 1
            continue

        # начало нашего ответа: ищем его подпись до конца текста
        end = next((j for j in range(index + 1, len(lines)) if _OWN_REPLY_END.match(lines[j])), None)

        # Подписи нет — клиент обрезал письмо или пользователь стёр её руками.
        # Снимается только строка с меткой: отбрасывать хвост вслепую значило бы потерять вопрос пользователя ниже.
        if end is None:
            index += 1
            continue

        # разделитель подписи "-- " стоит внутри найденного диапазона
        # и уходит вместе с ним
        index = end + 1

    # на месте вырезанного ответа остаются обрамлявшие его пустые строки
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# Убирает из текста шапки цитат «От:/Отправлено:/Кому:/Тема:».
# Шапка внутри текста для модели — граница письма: то, что перед ней, читается как чужая переписка, и контекст сессии распадается.
def strip_header_blocks(text: str) -> str:
    """Вырезает из текста шапки цитат, сохраняя остальные строки."""
    lines = _split_lines(text)
    kept: List[str] = []
    index = 0

    while index < len(lines):
        offset = _block_start(lines, index)

        # строка без шапки переносится в результат целиком
        if offset is None:
            kept.append(lines[index])
            index += 1
            continue

        # текст пользователя перед шапкой сохраняется: почтовый клиент склеивает
        # его с шапкой без перевода строки
        head = lines[index][:offset].rstrip()
        if head:
            kept.append(head)

        # внутренний цикл проглатывает шапку целиком
        index += 1
        while index < len(lines):
            # строка с меткой принадлежит шапке
            if _label_positions(lines[index]):
                index += 1
                continue

            # пустая строка принадлежит шапке, когда метка есть в следующей
            # непустой строке в пределах _HEADER_BLOCK_GAP
            following = _next_nonblank(lines, index)
            if not lines[index].strip() and following is not None and _label_positions(following):
                index += 1
                continue

            # прочие строки завершают шапку, внешний цикл продолжает разбор
            break

    # на месте вырезанной шапки остаются обрамлявшие её пустые строки:
    # три и более перевода строки схлопываются до двух
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# результат разбора письма в форме, с которой работает pipeline.py
@dataclass
class IncomingEmail:
    message_id: str
    sender: str  # адрес в нижнем регистре
    sender_name: str
    subject: str  # тема как есть, декодированная
    title: str  # нормализованная тема = название сессии
    body: str  # текст без цитаты предыдущего письма
    in_reply_to: Optional[str]
    references: List[str] = field(default_factory=list)
    date: str = ""
    # Заголовки разговора Exchange: по thread_index/thread_topic Outlook и OWA собирают ветку письма.
    # Переносятся в исходящий ответ, письмо попадает в тот же разговор.
    thread_index: str = ""
    thread_topic: str = ""
    body_raw: str = ""  # тело до очистки — видно, где промахнулась эвристика цитат
    # Разметка html-части как есть — уходит в цитату ответа, письмо пользователя воспроизводится без потери начертания, таблиц, картинок.
    # В запрос к модели и в таблицу messages не попадает.
    body_html: str = ""
    is_tnef: bool = False  # тело в winmail.dat: пустой body объясняется форматом письма
    is_forward: bool = False  # письмо переслано: пустой body означает тред без вопроса
    attachments: List["Attachment"] = field(default_factory=list)

    # выход: идентификаторы предков от ближайшего к дальнему.
    # в этом порядке storage.find_session_by_message_ids ищет сессию треда
    @property
    def ancestor_ids(self) -> List[str]:
        """Отдаёт Message-ID предков письма в порядке приоритета поиска сессии."""
        # ближайший предок стоит первым
        ids = [self.in_reply_to] if self.in_reply_to else []

        # references идёт от корня к ближайшему предку, reversed даёт обратный порядок.
        # in_reply_to уже есть в этой цепочке и повторно в список не попадает.
        ids += [ref for ref in reversed(self.references) if ref != self.in_reply_to]
        return ids


# вход: значение заголовка в кодировке RFC 2047 (=?utf-8?B?...?=), None допустим.
# выход: строка unicode; при сбое декодирования возвращается исходное значение
def decode_mime_header(raw: Optional[str]) -> str:
    """Декодирует заголовок письма из формата RFC 2047 в unicode."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    # ветка битой кодировки: разбор письма продолжается с исходной строкой
    except Exception:
        log.warning("не удалось декодировать заголовок: %r", raw[:100])
        return raw.strip()


# вход: декодированная тема письма.
# выход: тема без префиксов ответа и пересылки; она же служит названием сессии
def normalize_subject(subject: str) -> str:
    """Снимает с темы письма префиксы Re:, Fwd: и их русские формы."""
    title = subject.strip()

    # префиксы снимаются по одному до исчерпания: почтовые клиенты накладывают
    # их каскадом («Re: Fwd: Re: тема»)
    while True:
        stripped = _SUBJECT_PREFIX.sub("", title, count=1)

        # выражение перестало совпадать, префиксов больше нет
        if stripped == title:
            break
        title = stripped

    # split и join схлопывают пробелы и переводы строк, которые вносит
    # перенос длинной темы в mime-заголовке
    return " ".join(title.split()) or NO_SUBJECT_TITLE


# вход: значение заголовка References либо In-Reply-To, None допустим.
# выход: идентификаторы в угловых скобках в порядке появления в строке
def parse_message_ids(raw: Optional[str]) -> List[str]:
    """Разбирает заголовок треда в список идентификаторов писем."""
    if not raw:
        return []
    return re.findall(r"<[^<>\s]+>", raw)


# Снимает теги с html-части и останавливается на первом блоке цитаты.
# Цитата лежит внутри опознаваемого контейнера (blockquote, gmail_quote, divRplyFwdMsg), поэтому граница определяется точнее, чем построчными признаками в тексте.
class _HTMLTextExtractor(HTMLParser):
    # теги, дающие перевод строки в собираемом тексте
    _BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    # содержимое этих тегов в текст не попадает
    _SKIP = {"style", "script", "head"}

    # значения атрибута id у контейнеров цитаты: Outlook и почтовые редакторы
    _QUOTE_IDS = {"divrplyfwdmsg", "appendonlast", "stopspelling", "mail-editor-reference-message-container"}

    # подстроки атрибута class у контейнеров цитаты: Gmail, Yahoo,
    # Thunderbird, ProtonMail
    _QUOTE_CLASSES = ("gmail_quote", "yahoo_quoted", "moz-cite-prefix", "protonmail_quote")

    def __init__(self) -> None:
        # convert_charrefs=True отдаёт html-сущности (&nbsp;, &amp;) готовым текстом
        super().__init__(convert_charrefs=True)

        # parts накапливает куски текста в порядке обхода документа
        self.parts: List[str] = []

        # глубина вложенности внутри тега из набора _SKIP
        self._skip_depth = 0

        # признак встреченного начала цитаты: сбор текста прекращён
        self._done = False

    # выход: True, когда тег открывает цитату предыдущего письма
    def _is_quote_start(self, tag: str, attrs) -> bool:
        """Определяет, открывает ли тег контейнер цитаты."""
        # blockquote служит контейнером цитаты у всех клиентов
        if tag == "blockquote":
            return True

        # атрибуты приводятся к нижнему регистру: html-разметка регистр не различает
        values = {k.lower(): (v or "").lower() for k, v in attrs}
        if values.get("id", "") in self._QUOTE_IDS:
            return True

        # класс сверяется вхождением подстроки: клиенты дописывают к нему
        # собственные классы через пробел
        klass = values.get("class", "")
        return any(marker in klass for marker in self._QUOTE_CLASSES)

    def handle_starttag(self, tag, attrs):
        # после начала цитаты обработка тегов прекращается
        if self._done:
            return

        # тег цитаты завершает сбор текста
        if self._is_quote_start(tag, attrs):
            self._done = True
            return

        # вход в тег из _SKIP увеличивает глубину пропуска
        if tag in self._SKIP:
            self._skip_depth += 1

        # блочный тег даёт перевод строки в тексте
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._done:
            return

        # выход из тега _SKIP уменьшает глубину; проверка счётчика закрывает
        # случай непарного закрывающего тега
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        # текст внутри цитаты и внутри тегов _SKIP пропускается
        if self._done or self._skip_depth:
            return
        self.parts.append(data)

    # выход: собранный текст с нормализованными пробелами
    def text(self) -> str:
        """Склеивает собранные куски в текст."""
        raw = "".join(self.parts)

        # неразрывный пробел заменяется обычным: метод split его не распознаёт
        raw = raw.replace("\xa0", " ")

        # внутри каждой строки пробелы схлопываются, переводы строк сохраняются
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(lines)


# вход: разметка html-части письма.
# выход: текст без тегов, обрезанный по началу цитаты
def html_to_text(html: str) -> str:
    """Превращает html-часть письма в текст без цитаты."""
    parser = _HTMLTextExtractor()
    parser.feed(html)

    # close отдаёт парсеру остаток буфера
    parser.close()
    return parser.text()


# вход: часть MIME-сообщения. выход: её текст, пустая строка для части без содержимого.
# Заявленный charset не всегда верен: старые клиенты указывают utf-8 при windows-1251, часть писем приходит вовсе без charset.
def _decode_part(part: Message) -> str:
    """Декодирует байты части письма в текст перебором кодировок."""
    payload = part.get_payload(decode=True)

    # None приходит от части без тела
    if payload is None:
        return ""

    declared = part.get_content_charset()

    # перебор идёт строго, без errors="replace": первая кодировка, завершившая
    # декодирование успехом, и есть настоящая
    for charset in (declared, *_CHARSET_FALLBACKS):
        # declared равен None у части без указанного charset
        if not charset:
            continue
        try:
            return payload.decode(charset)
        # UnicodeDecodeError отмечает несовпадение кодировки, LookupError —
        # имя кодировки, неизвестное python
        except (UnicodeDecodeError, LookupError):
            continue

    # цепочка заканчивается на latin-1, который принимает любые байты; сюда
    # управление доходит при сбое перебора, порча символов допускается
    return payload.decode("utf-8", errors="replace")


# вход: MIME-сообщение. выход: текст письма, пустая строка если текстовых частей нет.
# text/plain в приоритете, text/html — запасной источник.
def extract_body(msg: Message) -> str:
    """Извлекает текст письма из его текстовых частей."""
    plain: Optional[str] = None
    html: Optional[str] = None

    # walk обходит дерево частей; для одночастного письма обход заменяется
    # списком из самого сообщения
    for part in msg.walk() if msg.is_multipart() else [msg]:
        # multipart-контейнер собственного тела не имеет
        if part.get_content_maintype() == "multipart":
            continue

        # вложенный файл .txt либо .html телом письма не является
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue

        # берётся первая часть каждого типа: последующие принадлежат цитате
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif ctype == "text/html" and html is None:
            html = _decode_part(part)

    # непустая часть text/plain отдаётся как есть
    if plain and plain.strip():
        return plain

    # пустая часть text/plain при заполненной html встречается у Outlook
    if html:
        return html_to_text(html)

    return ""


# вход: MIME-сообщение. выход: разметка первой части text/html без изменений, пустая строка если её нет.
# В отличие от extract_body html не переводится в текст: нужен для цитаты ответа целиком, с начертанием, таблицами, картинками.
def extract_html(msg: Message) -> str:
    """Достаёт разметку html-части письма без перевода в текст."""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue

        # вложенный файл .html телом письма не является
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue

        if part.get_content_type() == "text/html":
            return _decode_part(part)

    return ""


# вход: тема и тело письма до отсечения цитаты. выход: True для пересланного письма.
# У такого письма без своего текста тело восстанавливать нельзя — иначе чужой тред уйдёт в модель репликой пересылающего.
def is_forwarded(subject: str, raw_body: str) -> bool:
    """Определяет, переслано ли письмо пользователем."""
    # префикс темы задаёт клиент отправителя при нажатии «Переслать»
    if _FORWARD_SUBJECT.match(subject or ""):
        return True

    # Разделитель в теле остаётся и там, где префикс темы стёрли вручную.
    # Поиск — по первым 4000 знакам: разделитель стоит в начале тела, дальше начинается сам пересланный тред.
    return bool(_FORWARD_BODY.search((raw_body or "")[:4000]))


# вход: MIME-сообщение. выход: True, если тело лежит в winmail.dat.
# Такое письмо выглядит пустым — частей text/plain и text/html в нём нет.
def has_tnef(msg: Message) -> bool:
    """Определяет, приехало ли тело письма в контейнере TNEF."""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        # признак по mime-типу части
        if part.get_content_type().lower() in _TNEF_TYPES:
            return True

        # запасной признак по имени файла: почтовые шлюзы теряют mime-тип
        # при перекладывании письма
        filename = (part.get_filename() or "").lower()
        if filename == "winmail.dat":
            return True
    return False


def _content_id(part: Message) -> str:
    """Достаёт Content-ID части в виде, в каком на него ссылается html."""
    return str(part.get("Content-ID", "")).strip().strip("<>").strip().lower()


# ссылка на встроенную картинку: src="cid:image001.png@01DA..."
_CID_REF = re.compile(r"cid:([^\s\"'<>)]+)", re.IGNORECASE)


def _referenced_cids(msg: Message) -> set:
    """Собирает Content-ID картинок, встроенных в html письма."""
    found = set()
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() != "text/html":
            continue
        # ссылка в html бывает url-кодированной: cid:image001.png%4001DA
        found.update(unquote(ref).lower() for ref in _CID_REF.findall(_decode_part(part)))
    return found


# метка места картинки в тексте: символ области частного использования Unicode в письмах не встречается
_IMG_MARK = "\ue000"
_IMG_TOKEN = re.compile(rf"{_IMG_MARK}(\d+){_IMG_MARK}")

# строка завершающей формулы открывает подпись без разметки: Outlook в корпоративном
# контуре автоподпись закладкой _MailAutoSig не помечает. хвост до 60 знаков — имя в той же строке
_CLOSING_LINE = re.compile(
    r"^\s*(с уважением|с наилучшими пожеланиями|всего доброго|всего наилучшего|"
    r"best regards|kind regards|regards|sincerely)\b.{0,60}$",
    re.IGNORECASE,
)


class _InlineImageLocator(_HTMLTextExtractor):
    """Переводит html тела в текст с метками картинок cid: и останавливается на подписи."""

    # закладка Word, которой Outlook для Windows открывает автоподпись
    _SIGNATURE_ANCHORS = {"_mailautosig"}

    # id контейнера подписи: OWA и новый Outlook (префикс x_ OWA ставит внутри цитаты), Outlook mobile
    _SIGNATURE_IDS = {"signature", "x_signature", "ms-outlook-mobile-signature"}

    # подстроки class контейнера подписи: Gmail, Thunderbird
    _SIGNATURE_CLASSES = ("gmail_signature", "moz-signature")

    def __init__(self) -> None:
        """Готовит список Content-ID в порядке появления картинок в тексте."""
        super().__init__()
        self.cids: List[str] = []

    def _is_signature_start(self, tag: str, attrs) -> bool:
        """Определяет, открывает ли тег блок подписи."""
        values = {k.lower(): (v or "").lower() for k, v in attrs}
        if tag == "a" and values.get("name", "") in self._SIGNATURE_ANCHORS:
            return True
        if values.get("id", "") in self._SIGNATURE_IDS:
            return True
        klass = values.get("class", "")
        return any(marker in klass for marker in self._SIGNATURE_CLASSES)

    def handle_starttag(self, tag, attrs):
        """Ставит метку картинки cid: в текст; начало подписи завершает обход."""
        if not self._done and self._is_signature_start(tag, attrs):
            self._done = True
            return

        super().handle_starttag(tag, attrs)
        if self._done or self._skip_depth or tag != "img":
            return

        match = _CID_REF.match((dict(attrs).get("src") or "").strip())
        if match:
            self.parts.append(f"{_IMG_MARK}{len(self.cids)}{_IMG_MARK}")
            self.cids.append(unquote(match.group(1)).lower())


def _user_inline_cids(html: str) -> set:
    """Собирает Content-ID картинок, вставленных в текст письма над подписью."""
    if not html:
        return set()

    parser = _InlineImageLocator()
    parser.feed(html)
    parser.close()

    # метки снимаются до _quote_cut: эвристики цитаты рассчитаны на текст без них
    lines, where = [], {}
    for number, line in enumerate(_split_lines(parser.text())):
        clean, last = "", 0
        for token in _IMG_TOKEN.finditer(line):
            clean += line[last:token.start()]
            where[int(token.group(1))] = (number, len(clean))
            last = token.end()
        lines.append(clean + line[last:])

    # cut — граница по тексту: разделитель «-- », шапка цитаты, строка атрибуции
    cut, _, header_offset = _quote_cut(lines)
    closing = next((n for n, line in enumerate(lines[:cut]) if _CLOSING_LINE.match(line)), None)
    if closing is not None:
        cut, header_offset = closing, None

    return {
        parser.cids[index]
        for index, (number, offset) in where.items()
        if number < cut or (number == cut and header_offset is not None and offset < header_offset)
    }


# вход: MIME-сообщение. выход: список Attachment в порядке приложения файлов.
# Порядок сохраняется: пользователь ссылается на файлы по нему («по первому документу — вопрос такой»).
def extract_attachments(msg: Message) -> List[Attachment]:
    """Собирает файлы, приложенные пользователем к письму."""
    found: List[Attachment] = []
    referenced = _referenced_cids(msg)
    user_inline = _user_inline_cids(extract_html(msg))

    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue

        cid = _content_id(part)
        inline = bool(cid) and cid in user_inline
        filename = decode_mime_header(part.get_filename())

        # Apple Mail не даёт имени картинке, вставленной в текст
        if inline and not filename:
            ext = mimetypes.guess_extension(part.get_content_type()) or ""
            filename = f"image{len(found) + 1:03d}{ext}"

        # Часть без имени файла — тело письма и его alternative-варианты.
        # winmail.dat отсекается здесь отдельно: ответ по нему даёт has_tnef, а не «формат не поддерживается».
        if not filename or filename.lower() == "winmail.dat":
            continue

        # встроенная картинка подписи или цитаты опознаётся по ссылке cid: из html.
        # Exchange в mime_content ставит Content-ID на каждое вложение, включая приложенные.
        # Content-Disposition: inline Outlook ставит непоследовательно
        if not inline and cid in referenced:
            continue

        try:
            payload = part.get_payload(decode=True)
        # ветка битой base64-части: письмо обрабатывается без этого файла
        except Exception:
            log.warning("вложение %s не декодируется, пропускаю", filename)
            continue

        # пустое значение приходит от файла нулевой длины
        if not payload:
            continue

        found.append(Attachment(filename, part.get_content_type(), payload, inline=inline))
    return found


# вход: строки письма. выход: (номер строки цитаты, текст перед шапкой, смещение шапки внутри строки); cut==len(lines) — без цитаты.
# Вынесено отдельно: _split_at_quote ниже нужна не только текст перед цитатой, но и сама цитата (тело пересылки).
def _quote_cut(lines: Sequence[str]) -> Tuple[int, str, Optional[int]]:
    """Находит границу цитаты предыдущего письма и подписи."""
    cut = len(lines)
    tail = ""
    header_offset: Optional[int] = None

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

        # совпадение любого выражения с любым кандидатом завершает перебор
        if any(pattern.search(c) for pattern in _QUOTE_PATTERNS for c in candidates):
            cut = i
            break

    # Шапка цитаты ищется отдельным проходом, побеждает при равенстве номеров строк.
    # Ловит два случая, где построчные признаки молчат: шапка начинается с «Тема:», или клиент приклеил её к тексту без перевода строки.
    block = find_header_block(lines)
    if block is not None and block[0] <= cut:
        cut, header_offset = block
        tail = lines[cut][:header_offset].rstrip()

    return cut, tail, header_offset


# вход: текст письма. выход: (текст до цитаты, сама цитата до конца письма) — вторая часть нужна пересылке.
# strip_quoted ниже отдаёт только первую.
def _split_at_quote(text: str) -> Tuple[str, str]:
    """Делит текст письма на новый текст и цитату предыдущего письма."""
    lines = _split_lines(text)
    cut, tail, header_offset = _quote_cut(lines)

    kept = lines[:cut]
    # хвост строки cut дописывается последней строкой результата
    if tail:
        kept.append(tail)
    own_text = "\n".join(kept).strip()

    # Шапка стоит внутри строки cut вместе с текстом пользователя (tail); цитата начинается сразу после неё, offset их разделяет.
    # Без шапки строка cut целиком принадлежит цитате.
    if header_offset is not None:
        quote_lines = [lines[cut][header_offset:], *lines[cut + 1 :]]
    else:
        quote_lines = lines[cut:]
    quoted = "\n".join(quote_lines).strip()

    return own_text, quoted


# вход: текст письма. выход: текст до начала цитаты, обрезанный по краям.
# Цитата в реплику не пишется — тред уже лежит в messages отдельными репликами, повтор дал бы квадратичный рост запроса по длине сессии.
def strip_quoted(text: str) -> str:
    """Отсекает цитату предыдущего письма и подпись."""
    own_text, _ = _split_at_quote(text)
    return own_text


# вход: сообщение и адрес ящика модели. выход: причина отказа, None для письма от человека.
# Ответ на автоматическое письмо образует почтовую петлю.
def automated_reason(msg: Message, own_address: str) -> Optional[str]:
    """Определяет, по какой причине письмо остаётся без ответа."""
    sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()

    # адрес ящика модели в поле From: письмо вернулось к отправителю
    if sender and own_address and sender == own_address.lower():
        return "письмо от самого себя"

    # заголовок из reply_builder дошёл обратно во входящие
    if msg.get(LOOP_HEADER):
        return "письмо помечено как наше собственное — замкнулась петля"

    # тип multipart/report несут отчёты о доставке по RFC 3464
    if msg.get_content_type() == "multipart/report":
        return "отчёт о доставке (DSN)"

    # RFC 3834: любое значение, кроме "no", помечает автоматическое письмо
    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"

    # заголовок Precedence помечает рассылки и автоответы по конвенции,
    # действовавшей до RFC 3834
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "auto_reply", "junk"):
        return f"Precedence: {precedence}"

    # заголовки списков рассылки и автоответчиков отдельных клиентов
    for header in ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Autorespond"):
        if msg.get(header):
            return f"служебный заголовок {header}"

    # последняя проверка идёт по локальной части адреса и покрывает письма
    # без служебных заголовков
    local_part = sender.split("@")[0]
    if re.fullmatch(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce\S*", local_part):
        return f"адрес-автоответчик: {sender}"

    return None


# вход: сообщение без Message-ID. выход: <synthetic-...@local>.
# Message-ID обязателен по RFC 5322, но письма без него встречаются, а processed использует значение ключом.
def synthetic_message_id(msg: Message) -> str:
    """Строит заменитель Message-ID из полей, которые есть в любом письме."""
    # набор полей даёт одно и то же значение при повторном чтении папки
    seed = "|".join(
        [msg.get("From", ""), msg.get("Subject", ""), msg.get("Date", ""), msg.get("To", "")]
    )

    # sha256 усечён до 32 знаков: значение служит ключом внутри одной базы
    return f"<synthetic-{hashlib.sha256(seed.encode('utf-8', 'replace')).hexdigest()[:32]}@local>"


# вход: MIME-сообщение. выход: IncomingEmail со всеми полями.
# Побочных эффектов нет, сеть и база не используются.
def parse_email(msg: Message) -> IncomingEmail:
    """Разбирает письмо в структуру, с которой работает pipeline."""
    # parseaddr делит заголовок From на отображаемое имя и адрес
    sender_name, sender = email.utils.parseaddr(decode_mime_header(msg.get("From")))
    subject = decode_mime_header(msg.get("Subject"))

    # письмо без Message-ID получает суррогатный идентификатор
    message_id = (msg.get("Message-ID") or "").strip() or synthetic_message_id(msg)

    # заголовок In-Reply-To несёт один идентификатор по RFC, отдельные клиенты
    # кладут в него список; первым берётся ближайший предок
    in_reply_to_ids = parse_message_ids(msg.get("In-Reply-To"))

    raw_body = extract_body(msg)
    own_text, quoted = _split_at_quote(raw_body)
    forwarded = is_forwarded(subject, raw_body)

    if forwarded:
        # quoted — сам пересланный тред (не наша история); strip_quoted обрезал бы его целиком по своим признакам цитаты, поэтому здесь свой разбор.
        # strip_own_replies и strip_header_blocks не вызываются: шапки «От:/Кому:/Тема:» — единственный признак авторства реплик в чужом треде, без них модель не поймёт, кто что написал.
        thread = re.sub(r"\n{3,}", "\n\n", quoted).strip() if quoted.strip() else ""

        # own_text — текст перед разделителем пересылки, thread — сам тред;
        # оба непустых куска идут в тело через пустую строку
        body = "\n\n".join(part for part in (own_text, thread) if part).strip()
    else:
        body = own_text

        # Пустой результат при непустом теле — промах эвристики цитат: без текста вопрос выглядит как отказ модели отвечать.
        # strip_header_blocks убирает шапки (иначе реплика раздулась бы до размера треда), strip_own_replies — наш прежний ответ, уже лежащий в истории отдельной строкой.
        if len(body) < MIN_BODY_CHARS and raw_body.strip():
            log.warning(
                "отсечение цитаты дало пустой текст (%s), беру тело без шапок", message_id
            )
            body = strip_header_blocks(strip_own_replies(raw_body)) or raw_body.strip()

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
        body_html=extract_html(msg),
        # Thread-Index и Thread-Topic переносит в ответ reply_builder: Exchange
        # и Outlook собирают ветку разговора по ним
        thread_index=(msg.get("Thread-Index") or "").strip(),
        thread_topic=decode_mime_header(msg.get("Thread-Topic")),
        # признак TNEF вычисляется только у письма с пустым телом: наличие
        # winmail.dat при заполненном теле пользователю ничего не объясняет
        is_tnef=not body and has_tnef(msg),
        is_forward=forwarded,
        attachments=extract_attachments(msg),
    )
