# разбор входящего письма: заголовки, тело, отсечение цитаты, вложения.
# порядок: MIME-сообщение -> декодирование заголовков -> извлечение тела
# из части text/plain либо text/html -> отсечение цитаты предыдущего письма ->
# сбор вложений -> структура IncomingEmail.
# вход: объект email.message.Message от ews_client.fetch_unseen и от команды
# cli ingest-eml.
# выход: IncomingEmail с адресом, темой, названием сессии, телом,
# идентификаторами треда, заголовками разговора Exchange и списком вложений.
# класс Attachment импортируется из attachments.py.
# вызывается из pipeline.py и cli.py; константы REPLY_MARKER и LOOP_HEADER
# читает reply_builder.py.
# сеть и база данных здесь не используются: разбор прогоняется на .eml-файлах
# из tests/fixtures.

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

# технический маркер в подписи исходящих писем, ставит его reply_builder.py.
# формат исходящего письма задан проектом, поэтому граница цитаты по маркеру
# определяется точно.
# значение задано литералом и не выводится из MAIL_DISPLAY_NAME: переименование
# ящика оставило бы без границы ответы на уже отправленные письма
REPLY_MARKER = "[Sofi]"

# собственный заголовок исходящих писем. письмо с ним во входящих означает
# замкнувшуюся почтовую петлю
LOOP_HEADER = "X-Sofi"

# название сессии для письма с пустой темой
NO_SUBJECT_TITLE = "без темы"

# длина тела в символах, ниже которой отсечение цитаты считается промахом
MIN_BODY_CHARS = 2

# порядок перебора кодировок при неверном либо отсутствующем charset.
# cp1251 и koi8-r встречаются у российских почтовых клиентов до сих пор.
# latin-1 замыкает цепочку: декодирование в неё завершается успехом
# на любой последовательности байт
_CHARSET_FALLBACKS = ("utf-8", "cp1251", "koi8-r", "latin-1")

# префиксы ответа и пересылки; снимаются повторно, пока совпадает хоть один.
# "пер" ставит русский Outlook и OWA при пересылке
_SUBJECT_PREFIX = re.compile(
    r"^\s*(re|re\s*\[\d+\]|re\d+|fwd|fw|ответ|отв|пер|пересылаемое\s+сообщение)\s*:\s*",
    re.IGNORECASE,
)

# TNEF (winmail.dat) — контейнер Outlook в режиме формата RTF: тело письма
# приезжает внутри него, части text/plain и text/html отсутствуют.
# разбор контейнера в проекте не реализован; признак нужен, чтобы отличить
# такое письмо от письма с пустым телом — подсказки пользователю различаются
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
    # общий признак строки атрибуции: адрес в угловых скобках и двоеточие
    # в конце строки. в такой форме подписывают цитату Gmail в обеих локалях,
    # Яндекс и Mail.ru — одно выражение покрывает три клиента
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
# шапка «От:/Отправлено:/Кому:/Тема:» в этот список не входит: одиночная строка
# такой формы встречается в тексте пользователя («Кому: отделу продаж»),
# и отсечение по ней теряет часть письма. шапку целиком опознаёт
# find_header_block ниже

# разделитель подписи по RFC 3676: строка ровно из двух дефисов и пробела
_SIGNATURE_DELIMITER = re.compile(r"^\s*--\s?$")

# метки полей, из которых Outlook, OWA и Exchange собирают шапку цитаты.
# выражение допускает метку в середине строки: html_to_text склеивает текст
# пользователя с шапкой, когда клиент разделил их тегом <span>
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

# разделители, которыми клиенты открывают тело пересланного письма.
# divRplyFwdMsg приходит из html-разметки Outlook и доживает до текста после
# html_to_text, когда контейнер цитаты по нему не опознался
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


# вход: список строк и индекс текущей строки.
# выход: ближайшая непустая строка в пределах _HEADER_BLOCK_GAP строк вперёд,
# None при её отсутствии
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

    # одиночная метка засчитывается при метке в соседней строке. Outlook ставит
    # минимум три поля (От, Отправлено, Кому, Тема), поэтому признак срабатывает
    # на любом из них и не зависит от порядка полей
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


# строка, которой открывается наш прошлый ответ внутри цитаты: метка стоит
# первой в теле каждого исходящего письма (reply_builder.mark_answer).
# префикс допускает пробелы и знаки цитирования ">", которые дописывает клиент
_OWN_REPLY_START = re.compile(rf"^[\s>]*{re.escape(REPLY_MARKER)}\s")

# строка подписи исходящего письма: та же метка и разделитель " · ".
# подпись замыкает наш ответ, поэтому служит его нижней границей
_OWN_REPLY_END = re.compile(rf"^[\s>]*{re.escape(REPLY_MARKER)}\s*·")


# вход: текст письма после неудачного отсечения цитаты.
# выход: тот же текст без наших прошлых ответов.
# формат исходящего письма задан проектом: тело открывается меткой [Sofi]
# и замыкается подписью «[Sofi] · сессия «...»», поэтому границы нашего ответа
# внутри цитаты определяются точно, без эвристик.
# без этой вырезки ответ модели попадал бы в реплику пользователя вторым
# экземпляром: в таблице messages он уже лежит своей строкой
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

        # подписи нет — клиент обрезал письмо либо пользователь стёр её руками.
        # снимается одна строка с меткой: отбрасывать хвост текста вслепую
        # значило бы потерять вопрос пользователя, стоящий ниже
        if end is None:
            index += 1
            continue

        # разделитель подписи "-- " стоит внутри найденного диапазона
        # и уходит вместе с ним
        index = end + 1

    # на месте вырезанного ответа остаются обрамлявшие его пустые строки
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


# вход: текст письма после неудачного отсечения цитаты.
# выход: тот же текст без строк шапок «От:/Отправлено:/Кому:/Тема:».
# шапка внутри текста означает для модели границу письма: расположенное перед
# ней читается как чужая переписка, и контекст сессии распадается
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
    # заголовки разговора Exchange: по значениям thread_index/thread_topic
    # Outlook и OWA собирают ветку письма. reply_builder переносит их
    # в исходящий ответ, письмо попадает в тот же разговор Exchange
    thread_index: str = ""
    thread_topic: str = ""
    body_raw: str = ""  # тело до очистки — видно, где промахнулась эвристика цитат
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

        # references идёт от корня треда к ближайшему предку, reversed даёт
        # обратный порядок; значение in_reply_to присутствует в цепочке
        # и повторно в список не попадает
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


# снимает теги с html-части письма и останавливается на первом блоке цитаты.
# цитата в html-письме лежит внутри опознаваемого контейнера (blockquote
# у всех клиентов, gmail_quote у Gmail, divRplyFwdMsg у Outlook), поэтому
# её граница определяется точнее, чем построчными признаками в тексте
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


# вход: одна часть MIME-сообщения.
# выход: её текст; пустая строка для части без содержимого.
# заявленный charset соответствует содержимому не всегда: старые клиенты
# указывают utf-8 при отправке windows-1251, часть писем приходит без charset
# и кириллица разбирается как us-ascii
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


# вход: MIME-сообщение письма.
# выход: текст письма; пустая строка для письма без текстовых частей.
# часть text/plain имеет приоритет, text/html служит запасным источником
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


# вход: тема письма и его тело до отсечения цитаты.
# выход: True для письма, пересланного пользователем.
# признак нужен parse_email: у пересланного письма без собственного текста
# тело восстанавливать нельзя, иначе чужой тред целиком уходит в модель
# и в таблицу messages репликой пересылающего
def is_forwarded(subject: str, raw_body: str) -> bool:
    """Определяет, переслано ли письмо пользователем."""
    # префикс темы задаёт клиент отправителя при нажатии «Переслать»
    if _FORWARD_SUBJECT.match(subject or ""):
        return True

    # разделитель в теле остаётся и там, где префикс темы стёрли вручную;
    # поиск идёт по первым 4000 знакам: разделитель стоит в начале тела,
    # а дальше начинается сам пересланный тред
    return bool(_FORWARD_BODY.search((raw_body or "")[:4000]))


# вход: MIME-сообщение письма.
# выход: True для письма, тело которого лежит в контейнере winmail.dat.
# такое письмо выглядит пустым: частей text/plain и text/html в нём нет,
# и pipeline.py отвечает пользователю про формат письма
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


# вход: MIME-сообщение письма.
# выход: список Attachment в том порядке, в каком файлы приложены к письму.
# порядок сохраняется: пользователь ссылается на файлы по нему
# («по первому документу — вопрос такой»)
def extract_attachments(msg: Message) -> List[Attachment]:
    """Собирает файлы, приложенные пользователем к письму."""
    found: List[Attachment] = []

    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue

        filename = decode_mime_header(part.get_filename())

        # часть без имени файла образует тело письма и его alternative-варианты.
        # winmail.dat отсекается здесь: ответ по нему даёт has_tnef, и он
        # отличается от ответа «формат не поддерживается»
        if not filename or filename.lower() == "winmail.dat":
            continue

        disposition = str(part.get("Content-Disposition", "")).lower()

        # части с Content-ID образуют картинки внутри тела письма и подписи:
        # логотип компании, вставленный в текст скриншот. у делового письма
        # их набирается до десятка, к вопросу они отношения не имеют.
        # признаком служит Content-ID: Outlook помечает встроенную картинку
        # значением inline непоследовательно, ссылка из тела на неё
        # присутствует всегда. приложенная человеком картинка Content-ID
        # не имеет и доходит до разбора в attachments.py
        embedded = part.get("Content-ID") and (
            "inline" in disposition or part.get_content_maintype() == "image"
        )
        if embedded:
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

        found.append(Attachment(filename, part.get_content_type(), payload))
    return found


# вход: список строк письма.
# выход: (номер первой строки цитаты, текст пользователя из этой строки перед
# шапкой, смещение начала шапки внутри неё либо None для остальных признаков
# цитаты). значение cut, равное len(lines), обозначает письмо без цитаты.
# вынесено из strip_quoted отдельной функцией: _split_at_quote ниже нужен
# не только текст перед цитатой, но и сама цитата — тело пересланного письма
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

    # шапка цитаты ищется отдельным проходом и побеждает при равенстве номеров:
    # она находит начало цитаты в двух случаях, где построчные признаки молчат —
    # шапка начинается с поля «Тема:», и клиент приклеил шапку к тексту
    # пользователя без перевода строки
    block = find_header_block(lines)
    if block is not None and block[0] <= cut:
        cut, header_offset = block
        tail = lines[cut][:header_offset].rstrip()

    return cut, tail, header_offset


# вход: текст письма из extract_body.
# выход: (текст пользователя до цитаты, сама цитата от найденной границы
# и до конца письма). вторая часть нужна пересылке — strip_quoted ниже
# отдаёт только первую
def _split_at_quote(text: str) -> Tuple[str, str]:
    """Делит текст письма на новый текст и цитату предыдущего письма."""
    lines = _split_lines(text)
    cut, tail, header_offset = _quote_cut(lines)

    kept = lines[:cut]
    # хвост строки cut дописывается последней строкой результата
    if tail:
        kept.append(tail)
    own_text = "\n".join(kept).strip()

    # шапка стоит внутри строки cut вместе с текстом пользователя (tail);
    # цитата начинается сразу после неё, offset отделяет её от tail.
    # без шапки строка cut целиком принадлежит цитате
    if header_offset is not None:
        quote_lines = [lines[cut][header_offset:], *lines[cut + 1 :]]
    else:
        quote_lines = lines[cut:]
    quoted = "\n".join(quote_lines).strip()

    return own_text, quoted


# вход: текст письма из extract_body.
# выход: текст до начала цитаты, обрезанный по краям.
#
# в реплику сессии попадает только новый текст письма. цитата предыдущих писем
# в неё не пишется: тред целиком уже лежит в таблице messages отдельными
# репликами, и его повтор в каждой реплике дал бы рост запроса, квадратичный
# по длине сессии. служебные блоки «От:/Отправлено:/Кому:/Тема:» ставит
# почтовый сервер, эта функция использует их как границу цитаты
def strip_quoted(text: str) -> str:
    """Отсекает цитату предыдущего письма и подпись."""
    own_text, _ = _split_at_quote(text)
    return own_text


# вход: MIME-сообщение и адрес ящика модели.
# выход: текст причины отказа от ответа, None для письма от человека.
# ответ на автоматическое письмо образует почтовую петлю
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


# вход: MIME-сообщение без заголовка Message-ID.
# выход: идентификатор вида <synthetic-...@local>.
# заголовок Message-ID обязателен по RFC 5322, письма без него встречаются;
# журнал обработки в таблице processed использует это значение ключом
def synthetic_message_id(msg: Message) -> str:
    """Строит заменитель Message-ID из полей, которые есть в любом письме."""
    # набор полей даёт одно и то же значение при повторном чтении папки
    seed = "|".join(
        [msg.get("From", ""), msg.get("Subject", ""), msg.get("Date", ""), msg.get("To", "")]
    )

    # sha256 усечён до 32 знаков: значение служит ключом внутри одной базы
    return f"<synthetic-{hashlib.sha256(seed.encode('utf-8', 'replace')).hexdigest()[:32]}@local>"


# вход: MIME-сообщение письма.
# выход: IncomingEmail со всеми полями, заполненными функциями выше.
# побочные эффекты отсутствуют, сеть и база не используются
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
        # quoted у пересланного письма — сам пересланный тред, в истории этой
        # сессии он не лежит нигде: маркер REPLY_MARKER и разделитель
        # "---------- Forwarded message ---------" внутри него распознаются
        # _quote_cut как признаки цитаты, поэтому strip_quoted обрезал бы тред целиком
        # strip_own_replies здесь не вызывается: тред не дублирует историю
        # сессии, реплики модели внутри остаются текстом для модели.
        # шапки «От:/Отправлено:/Кому:/Тема:» тоже остаются: в пересланном
        # треде они единственный признак авторства реплик, и без них модель
        # получает несколько писем разных людей одним безымянным текстом
        # и не может ответить, кто что написал. strip_header_blocks здесь
        # не вызывается по этой причине; лишние пустые строки схлопываются,
        # чтобы шапки не расползались по объёму запроса
        thread = re.sub(r"\n{3,}", "\n\n", quoted).strip() if quoted.strip() else ""

        # own_text — текст перед разделителем пересылки, thread — сам тред;
        # оба непустых куска идут в тело через пустую строку
        body = "\n\n".join(part for part in (own_text, thread) if part).strip()
    else:
        body = own_text

        # пустой результат при непустом исходном теле означает промах
        # эвристики цитат: запрос без текста вопроса даёт ответ, который
        # читается как отказ модели отвечать.
        # тело восстанавливается через strip_header_blocks: тело без обработки
        # принесло бы в запрос и в таблицу messages шапки цитат вместе со всей
        # прежней перепиской треда, реплика выросла бы до размера треда
        # и вытеснила бы контекст сессии из следующего запроса.
        # strip_own_replies снимает наш прежний ответ внутри цитаты: письмо
        # своего же треда, где клиент подклеил его снизу, иначе дублировал бы
        # в истории сессии то, что там уже лежит своей строкой
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
