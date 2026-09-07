# проверка вложений письма перед отправкой их в Open WebUI.
# порядок: проверка веса и расширения -> mime-тип для загрузки -> безопасное имя.
# вход: объект Attachment, собранный email_parser.extract_attachments.
# выход: тот же объект, признанный пригодным; отказ поднимается
# как AttachmentError.
# пороги ATTACHMENT_* импортируются из config.py.
# вызывается из attachment_context.py; функцию selftest вызывает cli.py
# в команде check.
# сеть и база данных здесь не используются.
#
# извлечения текста в проекте нет. файл уходит в Open WebUI как есть, разбор
# и индексацию делает сервер: у него свой набор парсеров (Tika, Docling),
# распознавание текста и обновление этого набора без пересборки нашего образа.
# раньше текст извлекался здесь библиотеками unstructured и pdfminer.six —
# около гигабайта зависимостей в образе и второй, расходящийся с сервером,
# набор парсеров.
#
# из отказа от разбора следуют три вещи. страницы, объём текста и оглавление
# документа проекту неизвестны: в письме человеку называется вес файла.
# режим подачи документа проект не выбирает — фокусированный поиск включает
# сам Open WebUI, подачу целиком включает инструмент full_context_tool.py
# (см. src/tools/). в общее хранилище сервисной учётной записи попадает
# оригинал файла со всеми метаданными, а не результат извлечения.
#
# от вложения остались две проверки, обе дешёвые и обе о том, что до сервера
# доезжать не должно: вес файла и расширение

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from src.config import (
    ATTACHMENT_MAX_MB,
    MAX_ATTACHMENT_CONTEXT_CHARS,
)

log = logging.getLogger(__name__)


# отказ в обработке вложения; текст исключения pipeline.py дописывает
# к ответу пользователю примечанием
class AttachmentError(Exception):
    pass


# расширения, которые Open WebUI разбирает на своей стороне.
# набор шире прежнего: серверный разбор включает форматы Office 97-2003
# и распознавание текста на картинках, поэтому скан и .doc больше не отсекаются.
# архивы в набор не входят: внутри архива лежит ещё один слой вложений,
# и ответ по нему отличается от ответа про формат
SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".xls", ".ppt",
    ".csv", ".tsv", ".txt", ".md", ".rst",
    ".html", ".htm", ".xml", ".json", ".eml", ".msg",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".heic",
}

# mime-тип, с которым файл уходит в multipart, когда тип не определён
# ни письмом, ни расширением. Open WebUI выбирает парсер по имени файла,
# поэтому неизвестный тип разбору не мешает
DEFAULT_CONTENT_TYPE = "application/octet-stream"


# файл из письма: имя, mime-тип и байты содержимого.
# в Open WebUI уходят те же байты, что пришли в письме
@dataclass
class Attachment:
    filename: str
    content_type: str
    payload: bytes

    # выход: расширение в нижнем регистре; по нему check проверяет формат
    @property
    def suffix(self) -> str:
        """Отдаёт расширение файла в нижнем регистре."""
        return Path(self.filename).suffix.lower()

    # выход: вес вложения в мегабайтах, сравнивается с ATTACHMENT_MAX_MB
    @property
    def size_mb(self) -> float:
        """Считает вес вложения в мегабайтах."""
        return len(self.payload) / (1024 * 1024)

    # выход: вес вложения в байтах; это число пишется в session_files
    # и попадает в описание документа
    @property
    def size_bytes(self) -> int:
        """Отдаёт вес вложения в байтах."""
        return len(self.payload)

    # выход: mime-тип для поля multipart при загрузке.
    # заголовок письма приоритетнее расширения: отправитель мог переименовать
    # файл, а тип проставил почтовый клиент по содержимому
    @property
    def upload_type(self) -> str:
        """Выбирает mime-тип, с которым файл уходит в Open WebUI."""
        declared = (self.content_type or "").split(";")[0].strip().lower()

        # клиенты Outlook и мобильной почты ставят octet-stream на любое
        # вложение, поэтому такой тип уточняется по расширению
        if declared and declared != DEFAULT_CONTENT_TYPE:
            return declared

        guessed, _ = mimetypes.guess_type(self.filename)
        return guessed or DEFAULT_CONTENT_TYPE

    # выход: имя, под которым файл ложится в общее хранилище Open WebUI.
    # расширение сохраняется: по нему сервер выбирает парсер
    @property
    def upload_name(self) -> str:
        """Строит имя, под которым файл ложится в Open WebUI."""
        return safe_name(self.filename)

    # выход: строка веса для описания модели и для письма человеку
    @property
    def size_label(self) -> str:
        """Описывает файл его весом."""
        return size_words(self.size_bytes)


# вход: вес файла в байтах.
# выход: строка вида «412 КБ» либо «3.1 МБ»; «объём неизвестен» при нуле.
# нулевое значение стоит у файлов, записанных до перехода на серверный разбор
def size_words(size: int) -> str:
    """Описывает вес файла единицами, читаемыми в письме."""
    if size <= 0:
        return "объём неизвестен"

    # мегабайты появляются от 1 МБ: «1434 КБ» в письме читается хуже, чем «1.4 МБ»
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} МБ"

    # килобайты округляются до целого: доли килобайта смысла не несут
    return f"{max(1, round(size / 1024))} КБ"


# вход: объект Attachment из email_parser.extract_attachments.
# выход: тот же объект; отказ поднимается как AttachmentError с причиной,
# пригодной для показа пользователю.
# побочные эффекты отсутствуют, сеть не используется
def check(attachment: Attachment) -> Attachment:
    """Проверяет вложение письма перед загрузкой в Open WebUI."""
    suffix = attachment.suffix

    # предел веса держит и трафик до Open WebUI, и время его разбора:
    # файл на сотню мегабайт занимает поток обработки на минуты
    if attachment.size_mb > ATTACHMENT_MAX_MB:
        raise AttachmentError(
            f"файл больше {ATTACHMENT_MAX_MB} МБ ({attachment.size_mb:.0f} МБ)"
        )

    # пустое вложение сервер примет и проиндексирует пустотой: ответ по нему
    # будет собран из ничего и по виду не отличится от ответа по документу
    if not attachment.payload:
        raise AttachmentError("файл пустой")

    # расширения вне набора SUPPORTED_SUFFIXES: архивы, исполняемые файлы,
    # файлы без расширения
    if suffix not in SUPPORTED_SUFFIXES:
        raise AttachmentError(f"формат {suffix or 'без расширения'} не поддерживается")

    log.info(
        "вложение %s: %s, тип %s — уходит в Open WebUI",
        attachment.filename, attachment.size_label, attachment.upload_type,
    )
    return attachment


# вход: проверенное вложение.
# выход: текст описания, который pipeline.py ставит в запрос перед вопросом.
# описание объясняет модели происхождение текста, которого пользователь
# не писал, и называет документ по имени: вопрос «что в акте» без описания
# не связывается ни с одним из приложенных файлов
def describe(attachment: Attachment) -> str:
    """Собирает описание документа для текста запроса к модели."""
    return (
        f"К письму приложен документ «{attachment.filename}» "
        f"({attachment.size_label}); его содержимое доступно поиском."
    )


# вход: описания документов из describe; limit — предел блока в символах,
# 0 и меньше снимают ограничение.
# выход: блок текста с пустой строкой на конце; пустая строка при пустом списке.
# предел нужен потому, что блок встаёт в запрос перед текстом письма и входит
# в MAX_CONTEXT_CHARS
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


# выход: строка с числом форматов и действующими пределами.
# поднимает AttachmentError при отказе проверки на синтетическом файле.
# проверка дешёвая: разбора здесь больше нет, и падать этому пути негде,
# кроме опечатки в наборе расширений
def selftest() -> str:
    """Проверяет готовность проверки вложений для команды check."""
    probe = check(Attachment("проверка.txt", "text/plain", "Проверка вложений.".encode()))
    if probe.upload_type != "text/plain":
        raise AttachmentError(f"mime-тип определён неверно: {probe.upload_type}")

    return (
        f"форматов {len(SUPPORTED_SUFFIXES)}, до {ATTACHMENT_MAX_MB} МБ на файл; "
        "разбор и поиск по документу выполняет Open WebUI"
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
