# подготовка вложений письма к запросу: сеть, база и тексты примечаний.
# порядок разбит на две фазы. upload_attachments разбирает файлы и грузит их
# в Open WebUI, работает без замка сессии и занимает до
# ATTACHMENT_PROCESS_TIMEOUT_SEC секунд на файл. build_context пишет строки
# в session_files, читает файлы прежних писем треда и собирает блок описаний;
# эта фаза выполняется под замком сессии и обходится без сетевых вызовов.
# вход: IncomingEmail из email_parser.py и идентификатор сессии.
# выход: AttachmentContext со ссылками для запроса, блоком описаний перед
# вопросом, примечаниями пользователю и списком загруженных идентификаторов.
# локальный разбор документа выполняет attachments.py, загрузку и удаление —
# owui_files.py, строки таблицы session_files пишет storage.py.
# вызывается из pipeline.py.
#
# модуль отделяет сетевую и дисковую работу с вложениями от оркестрации
# в pipeline.py и от локального разбора в attachments.py, который сети
# и базы не касается

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from src import storage
from src.config import (
    ATTACHMENT_FULL_CONTEXT_CHARS,
    ATTACHMENT_FULL_CONTEXT_PAGES,
    ATTACHMENT_MAX_CHARS,
    ATTACHMENT_MAX_COUNT,
    ATTACHMENT_MAX_SESSION_FILES,
    ATTACHMENTS_ENABLED,
    LLM_WEB_URL,
)
from src.email_parser import IncomingEmail

log = logging.getLogger(__name__)

# примечания про вложения уходят пользователю всегда, когда с файлом возникла
# проблема и когда документ подан поиском: ответ по части документа по виду
# совпадает с ответом по всему документу, и по письму это различие не видно.
#
# тексты хранятся без обёртки, обёртку даёт ATTACHMENT_NOTE. те же слова
# работают вторым способом: когда до модели не дошло ни одного документа
# и ответа не будет, они уходят пользователю самостоятельным письмом
ATTACHMENT_NOTE = "\n\n(Примечание: {text})"
ATTACHMENT_SKIPPED_TEXT = "не удалось приложить к вопросу — {details}."
ATTACHMENT_FOCUSED_TEXT = (
    "{details} — отвечаю по релевантным фрагментам и оглавлению, "
    "а не по всему тексту. Уточняющий вопрос про конкретный раздел даст более точный ответ."
)
# отказ по документу без оглавления. файл здесь исправен, и вопрос по нему
# у человека остаётся, поэтому вместе с причиной уходят оба пути к ответу:
# без них следующим письмом приедет тот же файл
ATTACHMENT_TOO_LARGE_TEXT = (
    "{details} — обработать почтой не получилось. Документ не помещается "
    "в запрос целиком, а оглавления, по которому нашёлся бы нужный раздел, в нём нет: "
    "ответ собрался бы из случайных фрагментов, но выглядел бы как ответ по всему "
    "документу.{ways}"
)
# отказ по документу за пределом ATTACHMENT_MAX_CHARS. текст отдельный:
# оглавление на этот отказ не влияет, и его появление решения не изменит
ATTACHMENT_TOO_LONG_TEXT = (
    "{details} — обработать почтой не получилось: сервис берёт документы "
    "до {limit} знаков, а этот больше. Даже поиск по такому документу "
    "не дал бы ответа, за который можно ручаться.{ways}"
)


# результат сетевой фазы: документы, доехавшие до Open WebUI, и всё, что
# требуется сказать пользователю про остальные файлы письма
@dataclass
class UploadResult:
    # пары (разобранный документ, идентификатор файла в Open WebUI)
    uploaded: List[Tuple[Any, str]] = field(default_factory=list)
    # тексты про файлы, не попавшие в запрос по дефекту файла либо по сбою сети
    skipped: List[str] = field(default_factory=list)
    # исключения DocumentTooLargeError по исправным документам
    too_large: List[Any] = field(default_factory=list)

    # выход: идентификаторы загруженных файлов.
    # список нужен pipeline.py для уборки после прогона --dry-run
    @property
    def file_ids(self) -> List[str]:
        """Отдаёт идентификаторы файлов, загруженных при разборе письма."""
        return [file_id for _, file_id in self.uploaded]


# части запроса и ответа, которые дают вложения письма и файлы прежних писем треда
@dataclass
class AttachmentContext:
    files: List[Dict[str, Any]] = field(default_factory=list)  # ссылки для запроса
    prompt_prefix: str = ""  # описание документов перед вопросом
    notes: List[str] = field(default_factory=list)  # что сказать про вложения

    # выход: примечания в форме приписок к ответу модели
    @property
    def notice(self) -> str:
        """Собирает примечания припиской к тексту ответа."""
        return "".join(ATTACHMENT_NOTE.format(text=text) for text in self.notes)

    # выход: те же примечания как самостоятельный текст письма.
    # используется, когда ответа модели не будет вовсе, и приписка в скобках
    # оказалась бы приписана к пустому месту
    @property
    def standalone_notice(self) -> str:
        """Собирает примечания как текст отдельного письма."""
        return "\n\n".join(self.notes)


# вход: число страниц, признак оценки страниц и объём текста в знаках.
# выход: строка объёма для письма человеку.
# правило выбора совпадает с ParsedDocument.size_label, аргументы приходят
# числами: сюда они попадают из исключения либо из строки базы, и разобранного
# документа рядом уже нет
def _size_words(pages: int, pages_estimated: bool = False, chars: int = 0) -> str:
    """Описывает объём документа страницами и, при нужде, знаками."""
    words = f"{pages} стр.{' примерно' if pages_estimated else ''}"

    # знаки называются у документа, который велик именно объёмом: по отказу
    # на пять страниц без этого числа причина остаётся непонятной.
    # у файлов, записанных до появления колонки chars, значение нулевое,
    # и в строку попадают только страницы
    return f"{words}, {chars} знаков" if chars > ATTACHMENT_FULL_CONTEXT_CHARS else words


# вход: исключения DocumentTooLargeError, собранные при разборе вложений.
# выход: до двух примечаний, по одному на причину отказа.
# причины разделены: объём документа человек уменьшить может, на отсутствие
# оглавления повлиять не может
def _too_large_notes(documents: Sequence[Any]) -> List[str]:
    """Собирает примечания про документы, по которым ответ почтой не собрать."""
    from src.attachments import REASON_TOO_LONG

    # первый путь к ответу общий для обеих причин: прислать нужную часть
    # документа отдельным письмом
    ways = (
        " Что можно сделать: прислать отдельным письмом нужную часть документа "
        f"(до {ATTACHMENT_FULL_CONTEXT_PAGES} стр. и {ATTACHMENT_FULL_CONTEXT_CHARS} знаков) "
        "— по ней отвечу целиком"
    )

    # второй путь появляется вместе с адресом веб-интерфейса: LLM_WEB_URL
    # выводится из LLM_BASE_URL и бывает пустым, а ссылка без адреса
    # пользователю ничего не даёт
    ways += (
        f"; либо задать вопрос в веб-интерфейсе {LLM_WEB_URL} — там поиск идёт "
        "по документу целиком."
    ) if LLM_WEB_URL else "."

    notes = []
    for reason, template in (
        (REASON_TOO_LONG, ATTACHMENT_TOO_LONG_TEXT),
        (None, ATTACHMENT_TOO_LARGE_TEXT),  # остальные — «нет оглавления»
    ):
        # значение None в reason собирает все прочие причины отказа
        group = [exc for exc in documents
                 if (exc.reason == reason if reason else exc.reason != REASON_TOO_LONG)]
        if not group:
            continue

        # документы одной причины перечисляются в одном примечании
        details = "; ".join(
            f"«{exc.filename}» ({_size_words(exc.pages, exc.pages_estimated, exc.chars)})"
            for exc in group
        )
        notes.append(template.format(details=details, ways=ways, limit=ATTACHMENT_MAX_CHARS))
    return notes


# вход: строки таблицы session_files, полученные storage.get_session_files.
# выход: пара (ссылки для запроса, описания документов).
# документ из первого письма обсуждается и в пятом, поэтому ссылки на живые
# файлы сессии уходят в каждый запрос. оглавление читается из базы: текст
# документа проект не хранит
def _describe_stored(rows: Sequence) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Собирает ссылки и описания для файлов прежних писем треда."""
    from src import owui_files

    links, notes = [], []
    for row in rows:
        # sqlite хранит булево значение числом
        full = bool(row["full_context"])
        links.append(owui_files.reference(row["file_id"], full))

        # нулевое число страниц стоит у файлов, попавших в базу до появления
        # колонки chars
        size = _size_words(row["pages"], chars=row["chars"]) if row["pages"] else "объём неизвестен"

        # документ, поданный целиком, описывается одной строкой
        if full:
            notes.append(f"Ранее в переписке приложен документ «{row['filename']}» ({size}).")

        # документ, поданный поиском, несёт оглавление: по нему модель находит
        # нужный раздел
        else:
            outline = row["outline"] or ""
            head = (
                f"Ранее в переписке приложен документ «{row['filename']}» ({size}), "
                "доступен поиском по содержимому."
            )
            notes.append(f"{head} Структура документа:\n{outline}" if outline else head)
    return links, notes


# вход: объект Attachment из email_parser.extract_attachments.
# выход: пара (разобранный документ, идентификатор файла в Open WebUI).
# поднимает AttachmentError с причиной, пригодной для показа пользователю.
# побочный эффект: файл появляется в хранилище Open WebUI.
# строка в таблицу session_files здесь не пишется: запись идёт в build_context
# под замком сессии
def _upload_one(attachment) -> Tuple[Any, str]:
    """Разбирает вложение и кладёт его текст в Open WebUI."""
    from src import attachments, owui_files

    document = attachments.parse(attachment)

    # имя приводится к безопасному виду: оно уходит в общее хранилище
    file_id = owui_files.upload(attachments.safe_name(document.upload_name), document.text)

    try:
        owui_files.wait_processed(file_id)

    # ветка неудачной обработки: недообработанный файл на вопросы не отвечает,
    # занимает место в хранилище и попадает под те же глаза, что и рабочие
    except Exception:
        owui_files.delete(file_id)
        raise

    return document, file_id


# вход: разобранное письмо и режим примерки.
# выход: UploadResult с загруженными документами и текстами примечаний.
# побочные эффекты: сетевые запросы к Open WebUI, до
# ATTACHMENT_PROCESS_TIMEOUT_SEC секунд ожидания на файл.
# фаза выполняется без замка сессии: письма одного треда ждали бы здесь друг
# друга по несколько минут, не имея к документам отношения.
# сбой на одном файле ответ не отменяет: пользователь получает ответ
# по остальному письму и примечание о том, что приложить не удалось
def upload_attachments(incoming: IncomingEmail, dry_run: bool) -> UploadResult:
    """Разбирает вложения письма и загружает их текст в Open WebUI."""
    from src.attachments import AttachmentError, DocumentTooLargeError

    result = UploadResult()

    # ветка отключённой поддержки вложений: файлы остаются без обработки,
    # пользователь получает примечание
    if not ATTACHMENTS_ENABLED:
        if incoming.attachments:
            result.skipped.append("работа с вложениями отключена администратором")
        return result

    # имена файлов, уехавших с этим письмом на прошлой попытке: повторный разбор
    # после сбоя отправки создал бы их копии в общем хранилище.
    # чтение идёт по message_id письма и от сессии не зависит, поэтому замок
    # для него не нужен
    already = storage.filenames_for_message(incoming.message_id) if not dry_run else set()

    # срез отделяет файлы, попадающие в обработку, от остальных
    incoming_files = incoming.attachments[:ATTACHMENT_MAX_COUNT]
    result.skipped += [
        f"«{a.filename}»: за раз обрабатывается не больше {ATTACHMENT_MAX_COUNT} файлов"
        for a in incoming.attachments[ATTACHMENT_MAX_COUNT:]
    ]

    for attachment in incoming_files:
        # файл уже загружен прошлой попыткой обработки этого письма
        if attachment.filename in already:
            log.info(
                "вложение %s уже загружено этим письмом — беру прежний файл",
                attachment.filename,
            )
            continue

        try:
            result.uploaded.append(_upload_one(attachment))

        # ветка исправного документа, который сервис не берёт в работу:
        # объяснение пользователю занимает несколько строк, его собирает
        # _too_large_notes
        except DocumentTooLargeError as exc:
            log.warning("вложение %s не взято в работу: %s", attachment.filename, exc)
            result.too_large.append(exc)

        # ветка дефекта файла: формат, пароль, отсутствие текстового слоя
        except AttachmentError as exc:
            log.warning("вложение %s пропущено: %s", attachment.filename, exc)
            result.skipped.append(f"«{attachment.filename}»: {exc}")

        # ветка сбоя на стороне Open WebUI: сеть, отказ сервиса, таймаут
        # обработки. пользователю называется сервис, текст исключения остаётся
        # в логе
        except Exception as exc:
            log.error("вложение %s не загружено: %s", attachment.filename, exc)
            result.skipped.append(f"«{attachment.filename}»: сервис документов недоступен")

    return result


# вход: разобранное письмо, идентификатор сессии, результат сетевой фазы
# и режим примерки.
# выход: AttachmentContext со ссылками, блоком описаний и примечаниями.
# побочный эффект: строки в таблице session_files.
# предусловие: вызывается под замком сессии, чтобы письма одного треда писали
# свои файлы по очереди и каждое видело согласованный список прежних файлов
def build_context(
    incoming: IncomingEmail, session_id: int, upload: UploadResult, dry_run: bool
) -> AttachmentContext:
    """Собирает части запроса из загруженных документов и файлов прежних писем."""
    from src import attachments, owui_files
    from src.attachments import context_block

    context = AttachmentContext()
    descriptions: List[str] = []

    # focused собирает документы, поданные поиском: о режиме подачи пользователь
    # узнаёт из примечания к ответу
    focused: List[str] = []

    for document, file_id in upload.uploaded:
        # примерка строк в базу не пишет; сами файлы снимает pipeline
        # вызовом drop_uploaded
        if not dry_run:
            _remember(document, file_id, session_id, incoming.message_id)

        context.files.append(owui_files.reference(file_id, document.full_context))
        descriptions.append(attachments.describe(document))

        if not document.full_context:
            focused.append(f"документ «{document.filename}» ({document.size_label})")

    # прежние файлы треда читаются после записи новых, поэтому из выборки
    # исключаются идентификаторы этого письма: файл попал бы в запрос дважды —
    # ссылкой и описанием «ранее приложен»
    fresh = set(upload.file_ids)

    # остаток предела считается на весь запрос: отдельный предел на каждую
    # половину дал бы вдвое больше файлов, чем задано настройкой
    budget = max(0, ATTACHMENT_MAX_SESSION_FILES - len(context.files))
    stored = storage.get_session_files(session_id, ATTACHMENT_MAX_SESSION_FILES) if session_id else []
    stored_links, stored_notes = _describe_stored(
        [row for row in stored if row["file_id"] not in fresh][:budget]
    )
    context.files += stored_links
    descriptions += stored_notes

    # описания склеиваются в блок, который встанет перед текстом вопроса.
    # длину блока ограничивает MAX_ATTACHMENT_CONTEXT_CHARS: вместе с письмом
    # он входит в MAX_CONTEXT_CHARS и вытесняет историю сессии
    context.prompt_prefix = context_block(descriptions)

    # порядок примечаний: режим подачи, отказы по объёму, пропущенные файлы
    if focused:
        context.notes.append(ATTACHMENT_FOCUSED_TEXT.format(details=", ".join(focused)))
    if upload.too_large:
        context.notes.extend(_too_large_notes(upload.too_large))
    if upload.skipped:
        context.notes.append(ATTACHMENT_SKIPPED_TEXT.format(details="; ".join(upload.skipped)))

    return context


# вход: разобранный документ, идентификатор файла, сессия и Message-ID письма.
# побочный эффект: строка в таблице session_files.
# сбой записи снимает файл в Open WebUI перед подъёмом исключения: файл без
# строки в базе остаётся в общем хранилище навсегда — уборка по сроку хранения
# и команда forget работают по строкам таблицы
def _remember(document, file_id: str, session_id: int, message_id: str) -> None:
    """Записывает загруженный файл за сессией."""
    from src import owui_files

    try:
        storage.add_session_file(
            session_id, file_id, document.filename, document.pages,
            document.full_context, document.outline_text(), message_id,
            document.chars,
        )
    except Exception:
        log.error(
            "файл %s загружен в Open WebUI, но не записан в базу — удаляю его",
            file_id, exc_info=True,
        )
        owui_files.delete(file_id)
        raise


# вход: идентификаторы файлов, загруженных в этом прогоне примерки.
# побочный эффект: удаление файлов в Open WebUI.
# режим примерки состояние не меняет ни в базе, ни в ящике; файл в общем
# хранилище примерку тоже не переживает: десяток прогонов `once --dry-run`
# оставил бы десяток копий одного документа, а записей о них в базе нет
def drop_uploaded(file_ids: Sequence[str]) -> None:
    """Удаляет из Open WebUI файлы, загруженные в режиме примерки."""
    if not file_ids:
        return

    from src import owui_files

    for file_id in file_ids:
        owui_files.delete(file_id)
