# разбор вложений письма: документ превращается в текст, страницы и оглавление.
# порядок: проверка веса и расширения -> извлечение текста (pdfminer для pdf,
# unstructured для остальных форматов) -> разметка страниц -> сбор оглавления
# из заголовков -> проверка порогов подачи -> объект ParsedDocument.
# вход: объект Attachment, собранный email_parser.extract_attachments.
# выход: ParsedDocument с текстом, числом страниц, оглавлением и режимом подачи;
# отказ поднимается как AttachmentError либо DocumentTooLargeError.
# пороги ATTACHMENT_* импортируются из config.py.
# вызывается из pipeline.py; класс Attachment импортирует email_parser.py,
# функцию selftest вызывает cli.py в команде check.
# сеть и база данных здесь не используются: разбор прогоняется на .eml-файлах
# из tests/fixtures.
#
# текст извлекается локально, в Open WebUI уходит результат извлечения.
# благодаря этому число страниц считается по тому же тексту, который видит
# модель, набор парсеров на сервере на результат не влияет, а в общее хранилище
# сервисной учётной записи не попадает оригинал с макросами, скрытыми правками
# и метаданными автора.
#
# pdf разбирается библиотекой pdfminer.six: модуль unstructured.partition.pdf
# на импорте тянет unstructured_inference вместе с torch, transformers
# и onnxruntime, это около 4 ГБ в образ, разворачиваемый на вм с диском 40 ГБ.
# ту же pdfminer.six использует сама unstructured в режиме strategy="fast".
# типизацию элементов по извлечённому тексту делает partition_text.
# распознавание текста (OCR) в сборку не входит: скан без текстового слоя
# опознаётся пустым, и пользователь получает объяснение.
#
# режим подачи выбирают два порога — число страниц и объём текста. документ
# в пределах обоих уходит целиком (context=full), за пределами любого работает
# фокусированный поиск Open WebUI по собранному здесь оглавлению.
# порогов два, поскольку страница измеряет вёрстку: двадцать страниц регламента
# и двадцать страниц выгрузки различаются по объёму на порядок, а у форматов
# docx, xlsx и txt страницы вычисляются из того же текста.
# вес файла в письме порогом не служит: его держит ATTACHMENT_MAX_MB, и связь
# веса с объёмом текста слабая — скан на 2 МБ несёт одну страницу, docx
# на 200 КБ несёт пятьсот.
#
# страницы вычисляются самостоятельно для форматов, которые их не хранят.
# физические страницы есть у pdf. у docx, csv, txt и html поле page_number
# пустое всегда, включая docx с явными разрывами страниц; у pptx оно означает
# слайд, у xlsx — лист книги, и выгрузка на три тысячи строк приезжает тремя
# «страницами» по 80 000 знаков. поэтому разметка формата проверяется
# на плотность, и при её несоответствии документ размечается по объёму текста:
# страница равна CHARS_PER_PAGE знаков, куски текста (абзацы, строки таблицы)
# остаются целыми.
# этими же номерами подписаны маркеры страниц в выгружаемом тексте и строки
# оглавления: расхождение направило бы поиск по неверным координатам,
# а у документа без собственной нумерации все заголовки попали бы на «с. 1».
#
# документ за порогом подачи целиком проходит проверку оглавления. заголовки
# собираются из самого документа, и во внутренних файлах компании — сметах,
# выгрузках, сканах, сшитых из нескольких писем — они отсутствуют. поиск без
# оглавления отвечает по случайной части документа, и письмо с таким ответом
# по виду совпадает с ответом по всему документу. такой документ в работу
# не берётся: он остаётся вне Open WebUI, пользователь получает объяснение
# и два пути (прислать нужную часть отдельно, задать вопрос в веб-интерфейсе).
# см. DocumentTooLargeError.
#
# документ объёмом выше ATTACHMENT_MAX_CHARS в работу не берётся ни в одном
# режиме. требование к оглавлению растёт вместе с документом и упирается
# в предел самого оглавления (60 строк) примерно на миллионе знаков, после чего
# документ любого размера с шестьюдесятью заголовками проходит проверку.
# отказ по этому пределу называет пользователю причину REASON_TOO_LONG

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
    MAX_ATTACHMENT_CONTEXT_CHARS,
)

log = logging.getLogger(__name__)


# отказ в обработке вложения; текст исключения pipeline.py дописывает
# к ответу пользователю примечанием
class AttachmentError(Exception):
    pass


# причины, по которым сервис отказывается от исправного документа.
# письма по ним различаются: документ, поданный частями, обрабатывается;
# документ за пределом ATTACHMENT_MAX_CHARS не обрабатывается в любом виде
REASON_NO_OUTLINE = "no_outline"
REASON_TOO_LONG = "too_long"


# отказ от документа, с файлом которого проблем нет.
# отдельный тип нужен pipeline.py: прочие отказы называют дефект файла (формат,
# пароль, отсутствие текстового слоя) и укладываются в одну строку примечания,
# здесь же пользователю сообщается порядок дальнейших действий.
# текст письма выбирается по значению атрибута reason
class DocumentTooLargeError(AttachmentError):
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

        # текст исключения попадает в примечание к письму, поэтому причина
        # формулируется здесь
        super().__init__(
            f"{pages} стр., {chars} знаков — "
            + ("больше потолка на документ" if reason == REASON_TOO_LONG
               else "и ни одного заголовка, поиску не за что зацепиться")
        )


# расширения, которые разбирает сборка unstructured проекта (дополнения docx,
# pptx, xlsx, md) вместе с pdf через pdfminer.
# картинки и архивы в набор не входят: текст из картинки извлекается
# распознаванием, которого в сборке нет, а архив образует ещё один слой
# вложений, и ответ по нему отличается от ответа про формат
SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".tsv",
    ".txt", ".md", ".rst", ".html", ".htm", ".xml", ".json", ".eml", ".msg",
}

# расширения с собственным текстом отказа: проблему решает конкретное действие
# пользователя (пересохранение файла, документ с текстовым слоем)
_LEGACY_SUFFIXES = {".doc": "Word 97-2003", ".xls": "Excel 97-2003", ".ppt": "PowerPoint 97-2003"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".heic", ".webp"}

# длина условной страницы в знаках для форматов, которые страниц не хранят
# (docx, xlsx, txt): разбиение на страницы возникает у них при вёрстке
# под конкретный принтер.
# 1800 знаков составляют машинописную страницу — около 30 строк по 60 знаков.
# значение участвует только в сравнении с порогами, поэтому точность здесь
# на результат не влияет
CHARS_PER_PAGE = 1800

# предел плотности страницы, размеченной самим форматом. проверка нужна оттого,
# что поле page_number у unstructured означает страницу не во всех форматах:
# у pptx это слайд (в замерах проекта около 40 знаков), у xlsx — лист книги,
# и лист выгрузки на три тысячи строк приезжает одной «страницей»
# на 80 000 знаков. разрыв между этими случаями составляет четыре порядка,
# поэтому точное значение порога на результат не влияет: он отделяет разметку
# по страницам от нумерации другого смысла.
# разметка плотнее порога заменяется разметкой по символам (_pages_by_chars)
MAX_REAL_PAGE_CHARS = CHARS_PER_PAGE * 2

# пределы оглавления: оно уходит в каждый запрос по этому документу,
# и полсотни заголовков описывают структуру полностью
MAX_OUTLINE_ENTRIES = 60
# нижняя граница той же величины: сколько заголовков нужно документу за порогом
# подачи целиком, чтобы поиск по нему работал. два заголовка на документ
# образуют титул и колонтитул
MIN_OUTLINE_ENTRIES = 3
MAX_OUTLINE_CHARS = 4000
MAX_TITLE_CHARS = 120

# шаблон маркера страницы в выгружаемом тексте
_PAGE_MARKER = "--- страница {page} ---"
# длина текста страницы pdf, ниже которой на странице остался колонтитул
# с номером
_MIN_PAGE_CHARS = 3


# файл из письма до разбора: имя, mime-тип и байты содержимого
@dataclass
class Attachment:
    filename: str
    content_type: str
    payload: bytes

    # выход: расширение в нижнем регистре; по нему parse выбирает парсер
    @property
    def suffix(self) -> str:
        """Отдаёт расширение файла в нижнем регистре."""
        return Path(self.filename).suffix.lower()

    # выход: вес вложения в мегабайтах, сравнивается с ATTACHMENT_MAX_MB
    @property
    def size_mb(self) -> float:
        """Считает вес вложения в мегабайтах."""
        return len(self.payload) / (1024 * 1024)


# результат разбора документа: текст для Open WebUI и данные для его описания
@dataclass
class ParsedDocument:
    filename: str
    text: str
    pages: int
    outline: List[Tuple[int, str]] = field(default_factory=list)
    pages_estimated: bool = False  # страниц у формата нет, оценка по объёму

    # выход: длина извлечённого текста в знаках; этот же текст уходит в Open WebUI
    @property
    def chars(self) -> int:
        """Считает объём извлечённого текста в знаках."""
        return len(self.text)

    # выход: размер документа в единицах порогов по большей из двух мер.
    # страницы и объём измеряют документ разными линейками и расходятся в обе
    # стороны: плотная выгрузка на десяти страницах несёт больше текста,
    # чем презентация на ста. требование, растущее вместе с документом
    # (outline_usable), считается по той мере, которая для документа больше
    @property
    def weight_pages(self) -> int:
        """Приводит размер документа к страницам по большей из двух мер."""
        # линейкой служит CHARS_PER_PAGE: оглавление нужно поиску по тексту,
        # который в документе есть, и от значения порогов подачи не зависит
        return max(self.pages, math.ceil(self.chars / CHARS_PER_PAGE))

    # выход: True для документа, который уходит в модель целиком
    @property
    def full_context(self) -> bool:
        """Проверяет документ на оба порога подачи целиком."""
        # порог по страницам отсекает длинный документ, порог по объёму —
        # короткий и плотный: таблицу, выгрузку, текст без вёрстки
        return (
            self.pages <= ATTACHMENT_FULL_CONTEXT_PAGES
            and self.chars <= ATTACHMENT_FULL_CONTEXT_CHARS
        )

    # выход: строка объёма для описания модели и для письма человеку
    @property
    def size_label(self) -> str:
        """Описывает объём документа страницами и, при нужде, знаками."""
        # пометка «примерно» отмечает страницы, посчитанные по объёму текста
        pages = f"{self.pages} стр." + (" (примерно)" if self.pages_estimated else "")

        # знаки называются для документа, который велик именно объёмом:
        # запись «5 стр., 310 000 знаков» на короткой записке создаёт шум
        if self.chars > ATTACHMENT_FULL_CONTEXT_CHARS:
            return f"{pages}, {self.chars} знаков"
        return pages

    # выход: число различных заголовков оглавления.
    # повторы исключаются множеством: колонтитул опознаётся заголовком
    # на каждой странице, и по длине списка документ без разделов выглядел бы
    # размеченным лучше регламента (ср. outline_text)
    @property
    def outline_entries(self) -> int:
        """Считает различные заголовки в оглавлении документа."""
        return len({title for _, title in self.outline})

    # выход: True для оглавления, достаточного для фокусированного поиска
    @property
    def outline_usable(self) -> bool:
        """Проверяет, хватает ли заголовков для поиска по документу."""
        # требование растёт вместе с документом: один заголовок приходится
        # на ATTACHMENT_OUTLINE_PAGES_PER_ENTRY страниц. три заголовка
        # описывают структуру тридцати страниц, для двухсот их мало, и поиск
        # без оглавления возвращает случайные фрагменты в обоих случаях.
        # страницы берутся по большей из двух мер (weight_pages): по числу
        # своих страниц плотная выгрузка на десяти листах прошла бы проверку
        # тремя заголовками
        needed = max(
            MIN_OUTLINE_ENTRIES,
            math.ceil(self.weight_pages / ATTACHMENT_OUTLINE_PAGES_PER_ENTRY),
        )

        # требование ограничено сверху значением MAX_OUTLINE_ENTRIES: строки
        # оглавления сверх него отбрасываются в outline_text
        return self.outline_entries >= min(needed, MAX_OUTLINE_ENTRIES)

    # выход: имя файла в Open WebUI — исходное имя с расширением .txt.
    # расширение меняется намеренно: в хранилище лежит извлечённый текст,
    # и имя описывает содержимое файла точно
    @property
    def upload_name(self) -> str:
        """Строит имя, под которым текст документа ложится в Open WebUI."""
        return f"{Path(self.filename).stem}.txt"

    # выход: текст оглавления, строка на заголовок, в пределах обоих потолков
    def outline_text(self) -> str:
        """Собирает оглавление документа из заголовков без повторов."""
        seen = set()
        lines = []

        for page, title in self.outline:
            # повторы отбрасываются: колонтитул с названием документа
            # опознаётся заголовком на каждой странице и занял бы всё оглавление
            if title in seen:
                continue

            seen.add(title)
            lines.append(f"  с. {page} — {title}")

            # потолок по числу строк
            if len(lines) >= MAX_OUTLINE_ENTRIES:
                break

        # второй потолок ограничивает оглавление по символам
        return "\n".join(lines)[:MAX_OUTLINE_CHARS]


# вход: reported — значения page_number элементов; texts — их тексты.
# выход: True для разметки, соответствующей страницам по плотности.
# признаком служит плотность: страница несёт около страницы текста
def _pagination_is_real(reported: Sequence[Optional[int]], texts: Sequence[str]) -> bool:
    """Проверяет, означает ли нумерация формата разбиение по страницам."""
    # множество собирает непустые номера; формат без нумерации (docx, csv, txt)
    # даёт пустое множество
    pages = {page for page in reported if page}
    if not pages:
        return False

    # +1 на элемент учитывает перевод строки, которым куски склеиваются
    # при сборке текста
    chars = sum(len(text) + 1 for text in texts)
    return chars / len(pages) <= MAX_REAL_PAGE_CHARS


# вход: тексты кусков документа в порядке следования.
# выход: номер условной страницы для каждого куска, нумерация с 1.
# нумерация выполняется до сборки текста: этими же номерами подписываются
# маркеры страниц и строки оглавления, и расхождение направило бы поиск
# по неверным координатам
def _pages_by_chars(texts: Sequence[str]) -> List[int]:
    """Размечает документ условными страницами по объёму текста."""
    assigned, offset = [], 0

    for text in texts:
        # номер даётся по началу куска: абзацы и строки таблицы остаются
        # целыми. кусок длиннее страницы (csv приезжает одной таблицей
        # на десятки тысяч знаков) занимает столько страниц, сколько в нём
        # помещается, и следующий кусок получает номер за ним
        assigned.append(offset // CHARS_PER_PAGE + 1)
        offset += len(text) + 1

    return assigned


# вход: текст страницы либо документа.
# выход: список элементов unstructured с проставленными типами
def _partition_text(text: str):
    """Типизирует элементы текста средствами unstructured."""
    # импорт по месту вызова: unstructured тянет заметное дерево зависимостей,
    # и команды, работающие без вложений, эту загрузку пропускают
    from unstructured.partition.text import partition_text

    return partition_text(text=text)


# вход: строка, которую unstructured отнесла к типу Title.
# выход: True для строки, похожей на заголовок раздела.
# типизация работает по форме абзаца, поэтому строка «Пункт 3.1: срок
# рассмотрения не превышает десяти дней.» получает тип Title наравне с разделом
def _looks_like_heading(title: str) -> bool:
    """Отсеивает предложения, принятые типизацией за заголовок."""
    # признаки: длина в пределах MAX_TITLE_CHARS и отсутствие точки в конце.
    # ложные заголовки исчисляются сотнями и вытеснили бы из оглавления
    # настоящие разделы, которых в документе десятки
    return len(title) <= MAX_TITLE_CHARS and not title.endswith(".")


# вход: элемент unstructured и его текст; при text=None берётся str(element).
# выход: текст заголовка либо None для элемента другого типа
def _title_of(element, text: Optional[str] = None) -> Optional[str]:
    """Извлекает из элемента заголовок раздела."""
    # тип сверяется по имени класса: классы unstructured импортируются лениво
    if type(element).__name__ != "Title":
        return None

    # split и join схлопывают переводы строк и повторные пробелы внутри заголовка
    title = " ".join((str(element) if text is None else text).split())
    return title if title and _looks_like_heading(title) else None


# вход: элементы одной страницы и её номер.
# выход: пары (номер страницы, заголовок) для строк оглавления.
# номер приходит аргументом: документ к этому моменту размечен страницами
# формата либо страницами из _pages_by_chars, и метаданные элемента образовали
# бы второй источник нумерации
def _titles(elements, page: int) -> List[Tuple[int, str]]:
    """Отбирает заголовки среди элементов одной страницы."""
    found = []
    for element in elements:
        title = _title_of(element)
        if title:
            found.append((page, title))
    return found


# вход: байты pdf-файла и его имя.
# выход: ParsedDocument с текстом по страницам и оглавлением.
# поднимает AttachmentError при пароле, повреждении файла и отсутствии
# текстового слоя.
# страницы даёт pdfminer, типы элементов проставляет unstructured по её тексту
def _parse_pdf(data: bytes, filename: str) -> ParsedDocument:
    """Разбирает pdf постранично средствами pdfminer."""
    import io

    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        # extract_pages возвращает генератор макетов страниц, list разворачивает
        # его целиком: число страниц нужно до сборки текста
        layouts = list(extract_pages(io.BytesIO(data)))

    # ветка защищённого паролем файла
    except PDFPasswordIncorrect as exc:
        raise AttachmentError("файл защищён паролем") from exc

    # ветка повреждённой структуры файла и файла другого формата
    except PDFSyntaxError as exc:
        raise AttachmentError("файл повреждён или это не PDF") from exc

    chunks: List[str] = []
    outline: List[Tuple[int, str]] = []

    # нумерация с 1 уходит в маркеры страниц и в строки оглавления
    for number, layout in enumerate(layouts, start=1):
        # LTTextContainer отбирает текстовые блоки: картинки и векторная
        # графика в текст не попадают, распознавания в сборке нет
        page_text = "\n".join(
            element.get_text() for element in layout if isinstance(element, LTTextContainer)
        ).strip()

        # страница короче _MIN_PAGE_CHARS несёт колонтитул с номером
        if len(page_text) < _MIN_PAGE_CHARS:
            continue

        chunks.append(f"{_PAGE_MARKER.format(page=number)}\n{page_text}")
        outline += _titles(_partition_text(page_text), number)

    # пустой список означает файл без текстового слоя
    if not chunks:
        raise AttachmentError(
            "в файле нет текстового слоя — похоже, это скан. Распознайте документ "
            "(«Сохранить как PDF с текстом» в сканере или ABBYY) и пришлите заново"
        )

    # число страниц берётся из файла целиком, включая пропущенные пустые
    return ParsedDocument(filename, "\n\n".join(chunks), len(layouts), outline)


# вход: байты файла, его имя и расширение.
# выход: ParsedDocument с текстом, страницами и оглавлением.
# поднимает AttachmentError при отсутствии дополнения формата в сборке,
# при сбое разбора и при пустом тексте.
# страницы берутся из формата, когда его нумерация проходит проверку плотности;
# в остальных случаях документ размечается по объёму текста, поле
# pages_estimated поднимается, и в описании документа появляется «примерно»
def _parse_office(data: bytes, filename: str, suffix: str) -> ParsedDocument:
    """Разбирает документ штатным partition библиотеки unstructured."""
    import io

    from unstructured.partition.auto import partition

    try:
        # metadata_filename задаёт имя, по которому partition выбирает парсер
        elements = partition(file=io.BytesIO(data), metadata_filename=filename)

    except ImportError as exc:  # формат есть в списке, а extras в сборке нет
        raise AttachmentError(f"формат {suffix} не поддерживается этой сборкой") from exc

    # ветка прочих сбоев парсера: имя класса исключения попадает в примечание
    except Exception as exc:
        raise AttachmentError(f"не удалось разобрать файл ({type(exc).__name__})") from exc

    # пустые элементы отбрасываются сразу: они исказили бы и счёт страниц,
    # и разметку текста
    items = [(el, str(el)) for el in elements if str(el).strip()]
    if not items:
        raise AttachmentError("в файле не нашлось текста")

    texts = [text for _, text in items]
    reported = [el.metadata.page_number for el, _ in items]

    # нумерация формата не прошла проверку плотности: страницы считаются
    # по объёму текста
    estimated = not _pagination_is_real(reported, texts)

    # page_number приходит пустым и у формата с настоящей нумерацией, такой
    # элемент попадает на страницу 1
    assigned = _pages_by_chars(texts) if estimated else [page or 1 for page in reported]

    # у разметки по символам последний кусок может сам занимать несколько
    # страниц, поэтому итог считается по суммарному объёму текста
    pages = max(1, math.ceil(sum(len(t) + 1 for t in texts) / CHARS_PER_PAGE)) \
        if estimated else max(assigned)

    chunks: List[str] = []
    outline: List[Tuple[int, str]] = []

    # current хранит номер страницы, маркер которой уже поставлен
    current: Optional[int] = None

    for (element, text), page in zip(items, assigned):
        # маркер ставится на смену номера: элементы одной страницы идут подряд
        if page != current:
            chunks.append(_PAGE_MARKER.format(page=page))
            current = page

        chunks.append(text)

        # заголовки собираются попутно, с номером той страницы, на которую
        # попал элемент
        title = _title_of(element, text)
        if title:
            outline.append((page, title))

    return ParsedDocument(filename, "\n".join(chunks), pages, outline, pages_estimated=estimated)


# вход: объект Attachment из email_parser.extract_attachments.
# выход: ParsedDocument, готовый к загрузке в Open WebUI.
# поднимает AttachmentError с причиной отказа и DocumentTooLargeError
# для исправного документа, который сервис не берёт в работу.
# побочные эффекты отсутствуют, сеть не используется
def parse(attachment: Attachment) -> ParsedDocument:
    """Разбирает вложение письма в текст с разметкой страниц."""
    suffix = attachment.suffix

    # проверки по весу и расширению идут до разбора: они дешевле извлечения текста
    if attachment.size_mb > ATTACHMENT_MAX_MB:
        raise AttachmentError(
            f"файл больше {ATTACHMENT_MAX_MB} МБ ({attachment.size_mb:.0f} МБ)"
        )

    # картинка получает свой текст отказа: распознавания текста в сборке нет
    if suffix in _IMAGE_SUFFIXES:
        raise AttachmentError(
            "это изображение, а распознавания текста в сервисе нет — "
            "пришлите документ с текстовым слоем"
        )

    # форматы Office 97-2003 получают свой текст отказа: проблему решает
    # пересохранение файла
    if suffix in _LEGACY_SUFFIXES:
        raise AttachmentError(
            f"старый формат {_LEGACY_SUFFIXES[suffix]}: пересохраните файл "
            f"как {suffix}x и пришлите заново"
        )

    # прочие расширения вне набора SUPPORTED_SUFFIXES
    if suffix not in SUPPORTED_SUFFIXES:
        raise AttachmentError(f"формат {suffix or 'без расширения'} не поддерживается")

    # pdf разбирает pdfminer, остальные форматы — unstructured
    document = _parse_pdf(attachment.payload, attachment.filename) if suffix == ".pdf" \
        else _parse_office(attachment.payload, attachment.filename, suffix)

    # обе проверки ниже стоят здесь: документ, по которому ответа не будет,
    # остаётся вне общего хранилища Open WebUI и вне таблицы session_files.
    #
    # предел объёма проверяется первым: документ за ним в работу не берётся
    # при любом оглавлении, и в письме называется именно эта причина —
    # объём документа человек уменьшить может.
    #
    # проверка идёт после разбора: объём текста до извлечения неизвестен, вес
    # файла его не показывает (в 20 МБ docx помещаются сотни миллионов знаков).
    # предел бережёт Open WebUI от индексации такого документа, разбор
    # у нас держит ATTACHMENT_MAX_MB
    if ATTACHMENT_MAX_CHARS and document.chars > ATTACHMENT_MAX_CHARS:
        log.info(
            "вложение %s: %d знаков при потолке %d — отказ, документ не берётся",
            attachment.filename, document.chars, ATTACHMENT_MAX_CHARS,
        )
        raise DocumentTooLargeError(
            document.filename, document.pages, document.pages_estimated,
            document.chars, REASON_TOO_LONG,
        )

    # документ уходит в фокусированный поиск, оглавления для поиска не хватает
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


# вход: разобранный документ.
# выход: текст описания, который pipeline.py ставит в запрос перед вопросом.
# описание нужно обоим режимам: при context=full оно объясняет модели
# происхождение текста, которого пользователь не писал; при фокусированном
# поиске оно несёт оглавление, без которого на вопросы «о чём документ»
# и «есть ли раздел про X» модель отвечает по выдаче поиска
def describe(document: ParsedDocument) -> str:
    """Собирает описание документа для текста запроса к модели."""
    size = document.size_label

    # документ целиком описывается одной строкой: его текст модель видит весь
    if document.full_context:
        return f"К письму приложен документ «{document.filename}» ({size}); его текст доступен целиком."

    header = (
        f"К письму приложен документ «{document.filename}» ({size}). Он слишком велик, "
        "чтобы уместиться целиком: доступен поиск по его содержимому. Структура документа:"
    )
    outline = document.outline_text()

    # оглавление отсутствует у документа, прошедшего порог по объёму
    return f"{header}\n{outline}" if outline else header


# вход: описания документов из describe; limit — предел блока в символах,
# 0 и меньше снимают ограничение.
# выход: блок текста с пустой строкой на конце; пустая строка при пустом списке.
# предел нужен потому, что блок встаёт в запрос перед текстом письма и входит
# в MAX_CONTEXT_CHARS: описания десяти документов с оглавлениями по
# MAX_OUTLINE_CHARS знаков вытесняют из запроса всю историю сессии
def context_block(descriptions: Sequence[str], limit: int = MAX_ATTACHMENT_CONTEXT_CHARS) -> str:
    """Склеивает описания документов в блок перед текстом вопроса."""
    if not descriptions:
        return ""

    block = "\n\n".join(descriptions)

    # описания отбрасываются с конца: первыми в списке идут документы текущего
    # письма, документы прежних писем треда стоят за ними
    if limit > 0 and len(block) > limit:
        log.info(
            "блок описаний документов обрезан: %d знаков при пределе %d",
            len(block), limit,
        )
        block = block[:limit].rstrip()

    # хвостовые переводы строки отделяют блок от текста письма
    return block + "\n\n"


# выход: строка с числом форматов и действующими порогами.
# поднимает RuntimeError при пустом результате разбора и ImportError
# при отсутствии pdfminer.
# разбор опирается на unstructured и pdfminer, а те — на системные библиотеки
# и данные; проверка на живом письме обнаружила бы их нехватку после того,
# как письмо уже пришло
def selftest() -> str:
    """Проверяет готовность разбора вложений для команды check."""
    # разбор прогоняется на синтетическом txt: путь короткий и проходит через
    # partition, типизацию и сборку ParsedDocument
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


# всё, кроме букв, цифр, точки, дефиса, подчёркивания и пробела
_SAFE_NAME = re.compile(r"[^\w.\- ]", re.UNICODE)


# вход: имя файла из заголовка письма.
# выход: имя без путей и служебных символов; "attachment" для пустого результата.
# имя уходит в общее хранилище Open WebUI, поэтому разделители пути из него
# удаляются
def safe_name(filename: str) -> str:
    """Приводит имя файла к виду, пригодному для внешнего хранилища."""
    # Path().name отбрасывает и unix-, и windows-путь
    name = Path(filename).name
    return _SAFE_NAME.sub("_", name).strip() or "attachment"
