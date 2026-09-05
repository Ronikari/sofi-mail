"""Разбор вложений письма: документ -> текст, страницы, оглавление.

Здесь только локальная работа с файлом — ни сети, ни базы, поэтому весь разбор
прогоняется на сохранённых .eml-фикстурах, как и разбор тела письма.

Решения, определяющие модуль:

- **Разбирает unstructured, а не Open WebUI.** В Open WebUI уезжает уже
  извлечённый текст, а не оригинал. Так то, что мы посчитали страницами, и то,
  что увидит модель, — один и тот же текст; набор парсеров на сервере
  ни на что не влияет; а в общее хранилище сервисной учётки не попадает
  оригинал с макросами, скрытыми правками и метаданными автора.

- **PDF идёт мимо `unstructured.partition.pdf`.** Этот модуль на импорте тянет
  `unstructured_inference`, а с ним torch, transformers и onnxruntime — около
  четырёх гигабайт в образ, который едет в Nexus и разворачивается на ВМ с 40 ГБ
  диска. Поэтому страницы PDF извлекает `pdfminer.six` (ровно та библиотека,
  которой пользуется сама unstructured в режиме strategy="fast"), а типизацию
  элементов по её тексту делает `partition_text`. Плата за это — нет OCR:
  скан без текстового слоя честно опознаётся как пустой, и пользователю
  приходит понятное объяснение вместо молчаливого ответа ни о чём.

- **Стратегию решают страницы и объём текста, но не вес файла.** До
  `ATTACHMENT_FULL_CONTEXT_PAGES` страниц И `ATTACHMENT_FULL_CONTEXT_CHARS`
  знаков документ уходит целиком (`context=full`), дальше — фокусированный
  поиск Open WebUI, которому в помощь идёт собранное здесь оглавление: без
  него на вопросах «о чём документ» модель видит случайные фрагменты.
  Двух порогов здесь два, потому что страница — мера вёрстки, а не текста:
  двадцать страниц регламента и двадцать страниц выгрузки различаются по
  объёму на порядок, а у docx, xlsx и txt страниц нет вовсе и считаются они
  из того же текста. Вес файла в письме порогом не служит: его держит
  `ATTACHMENT_MAX_MB`, а к объёму текста он отношения не имеет — скан на
  2 МБ несёт одну страницу, docx на 200 КБ — пятьсот.

- **Страницу считаем сами там, где формат её не знает.** Физические страницы
  есть только у PDF. У docx, csv, txt и html `page_number` пуст всегда (у docx —
  даже при явных разрывах страниц), у pptx это слайд, а у xlsx — лист книги:
  выгрузка на три тысячи строк приезжает тремя «страницами» по 80 000 знаков.
  Поэтому разметка формата проверяется на плотность, и если её «страницы»
  страницами быть не могут, документ размечается заново по объёму текста:
  страница = CHARS_PER_PAGE знаков, куски (абзацы, строки таблицы) целиком.
  Этими же номерами подписаны маркеры страниц в выгружаемом тексте и строки
  оглавления — иначе оглавление указывало бы поиску не туда, а у документа
  без собственной нумерации все заголовки оказывались бы на «с. 1».
  Считается это по тексту, поэтому картинки в счёт не идут: OCR в сборке нет,
  модель видит ровно тот текст, по которому мы и меряем.

- **За любым из порогов оглавление обязательно.** Заголовки собираются из самого
  документа, и во внутренних файлах компании — сметах, выгрузках, сканах,
  сшитых из нескольких писем, — их часто нет вовсе. Тогда фокусированный поиск
  остаётся без карты и отвечает по тому, что попало в выдачу, а письмо с таким
  ответом неотличимо от ответа по всему документу. Проверить это по переписке
  нельзя, и цена ошибки тем выше, чем выше должность адресата, — поэтому такой
  документ не берётся в работу: он не уезжает в Open WebUI, а пользователь
  получает объяснение и два рабочих пути (прислать нужную часть отдельно или
  задать вопрос в веб-интерфейсе). См. `DocumentTooLargeError`.

- **Выше `ATTACHMENT_MAX_CHARS` документ не берётся вовсе.** Требование
  к оглавлению растёт вместе с документом, но упирается в потолок самого
  оглавления (60 строк) примерно на миллионе знаков — дальше документ любого
  объёма с шестьюдесятью заголовками проходил бы проверку и уезжал бы
  в Open WebUI индексироваться. Потолок закрывает этот участок: отказ идёт
  с той же парой рабочих путей, но с другой причиной (`REASON_TOO_LONG`).
"""

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from src.config import (
    ATTACHMENT_FULL_CONTEXT_CHARS,
    ATTACHMENT_FULL_CONTEXT_PAGES,
    ATTACHMENT_MAX_CHARS,
    ATTACHMENT_MAX_MB,
    ATTACHMENT_OUTLINE_PAGES_PER_ENTRY,
)

log = logging.getLogger(__name__)


class AttachmentError(Exception):
    """Вложение обработать нельзя. Текст пишется пользователю в примечании."""


# Почему сервис не взял документ, который сам по себе в порядке. Причин две,
# и письма по ним разные: в одном случае документ можно прислать частями и
# всё получится, в другом он велик настолько, что и поиск по нему сервис
# не потянет
REASON_NO_OUTLINE = "no_outline"
REASON_TOO_LONG = "too_long"


class DocumentTooLargeError(AttachmentError):
    """Документ в работу не взят, хотя с самим файлом всё в порядке.

    Свой тип, потому что и разговор с пользователем здесь другой. Остальные
    отказы — про файл: не тот формат, пароль, скан; их лечит одно действие,
    и укладываются они в одну строку примечания. Здесь же с файлом всё
    в порядке, не хватает сервиса — и сказать нужно, что делать дальше.
    Разбирать это по тексту сообщения пайплайну не пришлось бы: он ловит тип,
    а какую именно причину назвать в письме, выбирает по `reason`.
    """

    def __init__(
        self,
        filename: str,
        pages: int,
        pages_estimated: bool = False,
        chars: int = 0,
        reason: str = REASON_NO_OUTLINE,
    ):
        self.filename = filename
        self.pages = pages
        self.pages_estimated = pages_estimated
        self.chars = chars
        self.reason = reason
        super().__init__(
            f"{pages} стр., {chars} знаков — "
            + ("больше потолка на документ" if reason == REASON_TOO_LONG
               else "и ни одного заголовка, поиску не за что зацепиться")
        )


# Форматы, которые умеет разобрать наша сборка unstructured (extras docx, pptx,
# xlsx, md) плюс PDF через pdfminer. Картинки и архивы сюда намеренно не входят:
# без OCR из картинки текста не извлечь, а архив — это ещё один слой вложений,
# на который пользователю нужен другой ответ, а не «формат не поддерживается».
SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".tsv",
    ".txt", ".md", ".rst", ".html", ".htm", ".xml", ".json", ".eml", ".msg",
}

# Форматы, про которые пользователю нужен свой ответ: лечится не «пришлите
# другой файл», а конкретным действием
_LEGACY_SUFFIXES = {".doc": "Word 97-2003", ".xls": "Excel 97-2003", ".ppt": "PowerPoint 97-2003"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".heic", ".webp"}

# Сколько символов считаем страницей там, где страниц нет физически (docx,
# xlsx, txt): у этих форматов разбиение на страницы появляется только при
# вёрстке в конкретном принтере. 1800 — привычная машинописная страница
# (примерно 30 строк по 60 знаков); точность здесь и не нужна, значение
# участвует только в сравнении с порогом
CHARS_PER_PAGE = 1800

# Потолок плотности «настоящей» страницы: столько знаков ещё может нести
# страница, размеченная самим форматом. Проверка нужна из-за того, что
# `page_number` у unstructured — не всегда страница: у pptx это слайд (в наших
# замерах ~40 знаков), а у xlsx — ЛИСТ книги, и лист выгрузки на три тысячи
# строк приезжает одной «страницей» на 80 000 знаков. Разрыв между этими
# случаями — четыре порядка, поэтому точное значение здесь не важно: важно
# отделить разметку, которая действительно про страницы, от нумерации, которая
# про что-то другое. Пагинация плотнее этого порога — не пагинация, и документ
# размечается по символам заново (см. `_pages_by_chars`)
MAX_REAL_PAGE_CHARS = CHARS_PER_PAGE * 2

# Потолки на оглавление: оно уходит в каждый запрос по этому документу, и
# полсотни заголовков — уже больше, чем помогает
MAX_OUTLINE_ENTRIES = 60
# Пол для той же величины: сколько заголовков нужно документу за порогом,
# чтобы поиск по нему вообще имел смысл. Два заголовка на документ —
# это, как правило, титул и колонтитул, а не разделы
MIN_OUTLINE_ENTRIES = 3
MAX_OUTLINE_CHARS = 4000
MAX_TITLE_CHARS = 120

_PAGE_MARKER = "--- страница {page} ---"
# у PDF-страницы часто остаётся только колонтитул с номером — не текст
_MIN_PAGE_CHARS = 3


@dataclass
class Attachment:
    """Файл, приехавший в письме, как есть."""

    filename: str
    content_type: str
    payload: bytes

    @property
    def suffix(self) -> str:
        return Path(self.filename).suffix.lower()

    @property
    def size_mb(self) -> float:
        return len(self.payload) / (1024 * 1024)


@dataclass
class ParsedDocument:
    """Результат разбора: то, что уедет в Open WebUI, и чем это описать."""

    filename: str
    text: str
    pages: int
    outline: List[Tuple[int, str]] = field(default_factory=list)
    pages_estimated: bool = False  # страниц у формата нет, оценка по объёму

    @property
    def chars(self) -> int:
        """Объём извлечённого текста — того самого, что уедет в Open WebUI."""
        return len(self.text)

    @property
    def weight_pages(self) -> int:
        """Насколько документ велик в единицах порогов — по худшей из двух мер.

        Страницы и объём меряют одно и то же разными линейками и расходятся
        в обе стороны: у плотной выгрузки на десяти страницах текста больше,
        чем у ста страниц презентации. Требования, которые растут вместе
        с документом (`outline_usable`), должны считаться по той мере, по
        которой он велик, иначе документ, отправленный в поиск объёмом,
        получал бы требование к оглавлению по своим немногим страницам.

        Линейка здесь — CHARS_PER_PAGE, а не отношение порогов: карта нужна
        поиску по тому тексту, который в документе есть, и от того, сколько
        текста согласен принять целиком настройщик, она не зависит.
        """
        return max(self.pages, math.ceil(self.chars / CHARS_PER_PAGE))

    @property
    def full_context(self) -> bool:
        """Документ достаточно мал, чтобы уйти в модель целиком.

        Оба порога обязательны: страницы ловят длинный документ, объём —
        короткий, но плотный (таблица, выгрузка, сплошной текст без вёрстки).
        """
        return (
            self.pages <= ATTACHMENT_FULL_CONTEXT_PAGES
            and self.chars <= ATTACHMENT_FULL_CONTEXT_CHARS
        )

    @property
    def size_label(self) -> str:
        """Объём документа словами — для описания модели и письма человеку.

        Знаки называются только тогда, когда документ велик именно ими:
        в остальных случаях страницы понятнее, а «5 стр., 310 000 знаков»
        на записке — шум.
        """
        pages = f"{self.pages} стр." + (" (примерно)" if self.pages_estimated else "")
        if self.chars > ATTACHMENT_FULL_CONTEXT_CHARS:
            return f"{pages}, {self.chars} знаков"
        return pages

    @property
    def outline_entries(self) -> int:
        """Сколько разных заголовков в оглавлении.

        Считаются именно разные: колонтитул опознаётся заголовком на каждой
        странице, и по сырой длине списка документ без единого раздела выглядел
        бы размеченным лучше настоящего регламента (ср. `outline_text`).
        """
        return len({title for _, title in self.outline})

    @property
    def outline_usable(self) -> bool:
        """Хватает ли оглавления, чтобы вести по документу фокусированный поиск.

        Требование растёт вместе с документом: заголовок должен приходиться
        примерно на каждые ATTACHMENT_OUTLINE_PAGES_PER_ENTRY страниц (страниц
        по худшей из двух мер, см. `weight_pages`, — иначе плотная выгрузка
        на десяти листах прошла бы проверку тремя заголовками). Три
        заголовка — карта тридцати страниц и не карта двухсот, а поиск без
        карты возвращает случайные фрагменты одинаково уверенно в обоих
        случаях. Верхняя граница требования — MAX_OUTLINE_ENTRIES: выше него
        оглавление всё равно обрезается, и требовать недостижимого нельзя.
        """
        needed = max(
            MIN_OUTLINE_ENTRIES,
            math.ceil(self.weight_pages / ATTACHMENT_OUTLINE_PAGES_PER_ENTRY),
        )
        return self.outline_entries >= min(needed, MAX_OUTLINE_ENTRIES)

    @property
    def upload_name(self) -> str:
        """Имя файла в Open WebUI: исходное с суффиксом .txt.

        Расширение меняется намеренно — в хранилище лежит извлечённый текст,
        а не оригинал, и имя не должно обещать больше, чем там есть.
        """
        return f"{Path(self.filename).stem}.txt"

    def outline_text(self) -> str:
        """Оглавление: строка на заголовок, без повторов и в пределах потолков.

        Повторы убираются потому, что колонтитул с названием документа
        опознаётся как заголовок на каждой странице и в одиночку съел бы
        всё оглавление.
        """
        seen = set()
        lines = []
        for page, title in self.outline:
            if title in seen:
                continue
            seen.add(title)
            lines.append(f"  с. {page} — {title}")
            if len(lines) >= MAX_OUTLINE_ENTRIES:
                break
        return "\n".join(lines)[:MAX_OUTLINE_CHARS]


def _pagination_is_real(reported: Sequence[Optional[int]], texts: Sequence[str]) -> bool:
    """Похожа ли разметка формата на разбиение по страницам.

    Судим по плотности: страница — это примерно страница текста, а не весь
    документ под одним номером. Формат, у которого номеров нет вовсе (docx,
    csv, txt — `page_number` там пустой), сюда попадает с пустым множеством
    и честно получает False.
    """
    pages = {page for page in reported if page}
    if not pages:
        return False
    chars = sum(len(text) + 1 for text in texts)
    return chars / len(pages) <= MAX_REAL_PAGE_CHARS


def _pages_by_chars(texts: Sequence[str]) -> List[int]:
    """Номер условной страницы для каждого куска — разбиение по символам.

    Это и есть «страницы» там, где их нет физически: границей служит
    CHARS_PER_PAGE, а куски (абзацы, строки таблицы) не режутся посередине —
    номер даётся по тому, на какой странице кусок начинается. Поэтому один
    элемент длиннее страницы (csv приезжает единственной таблицей на десятки
    тысяч знаков) занимает столько страниц, сколько в нём помещается, и
    следующий за ним элемент получает номер уже за ним.

    Нумеровать нужно до сборки текста, а не после: этими же номерами
    подписываются маркеры страниц в выгружаемом тексте и строки оглавления,
    и разъехаться они не должны — оглавление служит поиску картой, а карта
    с чужими координатами хуже её отсутствия.
    """
    assigned, offset = [], 0
    for text in texts:
        assigned.append(offset // CHARS_PER_PAGE + 1)
        offset += len(text) + 1
    return assigned


def _partition_text(text: str):
    """Типизация элементов текста средствами unstructured.

    Импорт внутри функции: unstructured тянет за собой заметное дерево
    зависимостей, а команды вроде `sessions` и `--help` его ждать не должны.
    """
    from unstructured.partition.text import partition_text

    return partition_text(text=text)


def _looks_like_heading(title: str) -> bool:
    """Отсев обычных предложений, которые unstructured принял за заголовок.

    Типизация работает по форме абзаца, поэтому короткая строка «Пункт 3.1:
    срок рассмотрения не превышает десяти дней.» становится Title наравне
    с настоящим разделом. Признак простой и надёжный: заголовки не кончаются
    точкой. Оглавление из таких строк не помогает модели, а вытесняет из него
    настоящие разделы — их всего десятки, а «заголовков» бывают сотни.
    """
    return len(title) <= MAX_TITLE_CHARS and not title.endswith(".")


def _title_of(element, text: Optional[str] = None) -> Optional[str]:
    """Заголовок из элемента — или None, если это не заголовок раздела."""
    if type(element).__name__ != "Title":
        return None
    title = " ".join((str(element) if text is None else text).split())
    return title if title and _looks_like_heading(title) else None


def _titles(elements, page: int) -> List[Tuple[int, str]]:
    """Заголовки среди элементов одной страницы — строки будущего оглавления.

    Номер страницы задан снаружи: и PDF, и остальные форматы к этому моменту
    уже разложены по страницам (своим или посчитанным, см. `_pages_by_chars`),
    и брать его из метаданных элемента было бы вторым, несогласованным
    источником нумерации.
    """
    found = []
    for element in elements:
        title = _title_of(element)
        if title:
            found.append((page, title))
    return found


def _parse_pdf(data: bytes, filename: str) -> ParsedDocument:
    """PDF: страницы даёт pdfminer, типизацию — unstructured по её тексту."""
    import io

    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        layouts = list(extract_pages(io.BytesIO(data)))
    except PDFPasswordIncorrect as exc:
        raise AttachmentError("файл защищён паролем") from exc
    except PDFSyntaxError as exc:
        raise AttachmentError("файл повреждён или это не PDF") from exc

    chunks: List[str] = []
    outline: List[Tuple[int, str]] = []
    for number, layout in enumerate(layouts, start=1):
        page_text = "\n".join(
            element.get_text() for element in layout if isinstance(element, LTTextContainer)
        ).strip()
        if len(page_text) < _MIN_PAGE_CHARS:
            continue
        chunks.append(f"{_PAGE_MARKER.format(page=number)}\n{page_text}")
        outline += _titles(_partition_text(page_text), number)

    if not chunks:
        raise AttachmentError(
            "в файле нет текстового слоя — похоже, это скан. Распознайте документ "
            "(«Сохранить как PDF с текстом» в сканере или ABBYY) и пришлите заново"
        )
    return ParsedDocument(filename, "\n\n".join(chunks), len(layouts), outline)


def _parse_office(data: bytes, filename: str, suffix: str) -> ParsedDocument:
    """Всё остальное — штатный `partition` unstructured по расширению файла.

    Страницы здесь берутся из формата, только если они там есть и они —
    страницы. У docx, csv, txt и html `page_number` пуст всегда (у docx —
    даже при явных разрывах страниц), у pptx это слайд, а у xlsx — лист книги,
    который «страницей» лишь называется. Поэтому разметка формата сначала
    проверяется на плотность (`_pagination_is_real`), и не прошедший проверку
    документ размечается по объёму текста, как документ вовсе без страниц:
    страница = CHARS_PER_PAGE знаков (`_pages_by_chars`), `pages_estimated`
    поднят, и в описании к такому документу стоит «примерно».

    Считается это по извлечённому тексту, то есть картинки в счёт не идут:
    в docx из десяти листов скриншотов текста на строку, и «страниц» у него
    будет одна. Так и надо — модель видит ровно этот текст, OCR в сборке нет,
    а страницы нужны здесь для решения «целиком или поиском», а не для того,
    чтобы совпасть с нумерацией в Word.
    """
    import io

    from unstructured.partition.auto import partition

    try:
        elements = partition(file=io.BytesIO(data), metadata_filename=filename)
    except ImportError as exc:  # формат есть в списке, а extras в сборке нет
        raise AttachmentError(f"формат {suffix} не поддерживается этой сборкой") from exc
    except Exception as exc:
        raise AttachmentError(f"не удалось разобрать файл ({type(exc).__name__})") from exc

    items = [(el, str(el)) for el in elements if str(el).strip()]
    if not items:
        raise AttachmentError("в файле не нашлось текста")

    texts = [text for _, text in items]
    reported = [el.metadata.page_number for el, _ in items]
    estimated = not _pagination_is_real(reported, texts)
    assigned = _pages_by_chars(texts) if estimated else [page or 1 for page in reported]
    # у разметки по символам последний кусок может сам занимать несколько
    # страниц, поэтому итог считается по объёму, а не по номеру последнего
    pages = max(1, math.ceil(sum(len(t) + 1 for t in texts) / CHARS_PER_PAGE)) \
        if estimated else max(assigned)

    chunks: List[str] = []
    outline: List[Tuple[int, str]] = []
    current: Optional[int] = None
    for (element, text), page in zip(items, assigned):
        if page != current:
            chunks.append(_PAGE_MARKER.format(page=page))
            current = page
        chunks.append(text)
        title = _title_of(element, text)
        if title:
            outline.append((page, title))

    return ParsedDocument(filename, "\n".join(chunks), pages, outline, pages_estimated=estimated)


def parse(attachment: Attachment) -> ParsedDocument:
    """Вложение -> текст с разметкой страниц. AttachmentError — причина отказа."""
    suffix = attachment.suffix
    if attachment.size_mb > ATTACHMENT_MAX_MB:
        raise AttachmentError(
            f"файл больше {ATTACHMENT_MAX_MB} МБ ({attachment.size_mb:.0f} МБ)"
        )
    if suffix in _IMAGE_SUFFIXES:
        raise AttachmentError(
            "это изображение, а распознавания текста в сервисе нет — "
            "пришлите документ с текстовым слоем"
        )
    if suffix in _LEGACY_SUFFIXES:
        raise AttachmentError(
            f"старый формат {_LEGACY_SUFFIXES[suffix]}: пересохраните файл "
            f"как {suffix}x и пришлите заново"
        )
    if suffix not in SUPPORTED_SUFFIXES:
        raise AttachmentError(f"формат {suffix or 'без расширения'} не поддерживается")

    document = _parse_pdf(attachment.payload, attachment.filename) if suffix == ".pdf" \
        else _parse_office(attachment.payload, attachment.filename, suffix)

    # Обе проверки здесь, а не в пайплайне: тогда документ, по которому мы всё
    # равно не ответим, не успевает уехать в общее хранилище Open WebUI —
    # ни файлом, ни строкой в базе сессии.
    #
    # Потолок идёт первым: документ за ним не берётся ни при каком оглавлении,
    # и назвать в письме нужно именно эту причину — с оглавлением человек
    # ничего сделать не может, а с объёмом может.
    #
    # Проверяется он после разбора: сколько в файле текста, до извлечения
    # не знает никто — вес файла об этом не говорит (в 20 МБ docx помещаются
    # сотни миллионов знаков). Поэтому потолок бережёт не разбор у нас —
    # его держит ATTACHMENT_MAX_MB, — а Open WebUI, куда такой документ
    # не поедет, и ответ, который по нему всё равно не собрался бы
    if ATTACHMENT_MAX_CHARS and document.chars > ATTACHMENT_MAX_CHARS:
        log.info(
            "вложение %s: %d знаков при потолке %d — отказ, документ не берётся",
            attachment.filename, document.chars, ATTACHMENT_MAX_CHARS,
        )
        raise DocumentTooLargeError(
            document.filename, document.pages, document.pages_estimated,
            document.chars, REASON_TOO_LONG,
        )

    if not document.full_context and not document.outline_usable:
        log.info(
            "вложение %s: %d стр., %d символов, заголовков %d — отказ, "
            "поиску не за что зацепиться",
            attachment.filename, document.pages, document.chars, document.outline_entries,
        )
        raise DocumentTooLargeError(
            document.filename, document.pages, document.pages_estimated, document.chars
        )

    log.info(
        "вложение %s: %d стр.%s, %d символов, режим %s",
        attachment.filename, document.pages, " (оценка)" if document.pages_estimated else "",
        document.chars, "целиком" if document.full_context else "поиск",
    )
    return document


def describe(document: ParsedDocument) -> str:
    """Блок про документ, который уходит в текст запроса перед вопросом.

    Нужен обоим режимам, но по разным причинам. При `context=full` он объясняет
    модели, откуда взялся текст, которого пользователь не писал. При
    фокусированном поиске он несёт оглавление — без него на вопросах «о чём
    документ» и «есть ли раздел про X» модель видит только те фрагменты,
    которые вытащил поиск, и отвечает мимо.
    """
    size = document.size_label
    if document.full_context:
        return f"К письму приложен документ «{document.filename}» ({size}); его текст доступен целиком."

    header = (
        f"К письму приложен документ «{document.filename}» ({size}). Он слишком велик, "
        "чтобы уместиться целиком: доступен поиск по его содержимому. Структура документа:"
    )
    outline = document.outline_text()
    return f"{header}\n{outline}" if outline else header


def context_block(descriptions: Sequence[str]) -> str:
    """Описания всех документов запроса одним блоком перед вопросом."""
    if not descriptions:
        return ""
    return "\n\n".join(descriptions) + "\n\n"


def selftest() -> str:
    """Проверка готовности разбора — для команды `check`.

    Разбор держится на unstructured и pdfminer, а те тянут за собой системные
    библиотеки и данные. Узнать, что чего-то не хватает, на живом письме
    должностного лица — худший момент: письмо уже пришло, ответа человек ждёт,
    а в логе ImportError.
    """
    document = parse(Attachment("проверка.txt", "text/plain", "Проверка разбора.".encode()))
    if not document.text:
        raise RuntimeError("разбор вернул пустой текст")

    import pdfminer.high_level  # noqa: F401  PDF идёт мимо unstructured, см. шапку

    return (
        f"форматов {len(SUPPORTED_SUFFIXES)}, "
        f"до {ATTACHMENT_FULL_CONTEXT_PAGES} стр. и {ATTACHMENT_FULL_CONTEXT_CHARS} знаков целиком, "
        f"дальше поиском по оглавлению (заголовок на {ATTACHMENT_OUTLINE_PAGES_PER_ENTRY} стр., "
        f"не меньше {MIN_OUTLINE_ENTRIES}), без оглавления — отказ, "
        + (f"потолок на документ {ATTACHMENT_MAX_CHARS} знаков"
           if ATTACHMENT_MAX_CHARS else "потолка на документ нет")
    )


_SAFE_NAME = re.compile(r"[^\w.\- ]", re.UNICODE)


def safe_name(filename: str) -> str:
    """Имя файла без путей и служебных символов — уезжает в чужое хранилище."""
    name = Path(filename).name
    return _SAFE_NAME.sub("_", name).strip() or "attachment"
