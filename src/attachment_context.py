# подготовка вложений письма к запросу: сеть, база и тексты примечаний.
# порядок разбит на две фазы. upload_attachments проверяет файлы и грузит их
# в Open WebUI, работает без замка сессии и занимает до
# ATTACHMENT_PROCESS_TIMEOUT_SEC секунд на файл. build_context пишет строки
# в session_files, читает файлы прежних писем треда и собирает блок описаний;
# эта фаза выполняется под замком сессии и обходится без сетевых вызовов.
# вход: IncomingEmail из email_parser.py и идентификатор сессии.
# выход: AttachmentContext со ссылками для запроса, блоком описаний перед
# вопросом, примечаниями пользователю и списком загруженных идентификаторов.
# проверку вложения выполняет attachments.py, загрузку и удаление —
# owui_files.py, строки таблицы session_files пишет storage.py.
# вызывается из pipeline.py.
#
# модуль отделяет сетевую и дисковую работу с вложениями от оркестрации
# в pipeline.py и от проверки в attachments.py, который сети и базы не касается.
#
# разбор документа выполняет Open WebUI: сюда уезжают байты файла, обратно
# приходит идентификатор. поэтому страниц, объёма текста и оглавления модуль
# не знает и режим подачи документа не выбирает — фокусированный поиск
# включает сам сервер, подачу целиком включает инструмент full_context_tool.py
# (см. src/tools/)

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from src import storage
from src.config import (
    ATTACHMENT_MAX_COUNT,
    ATTACHMENT_MAX_SESSION_FILES,
    ATTACHMENTS_ENABLED,
)
from src.email_parser import IncomingEmail

log = logging.getLogger(__name__)

# примечания про вложения уходят пользователю, когда с файлом возникла
# проблема: по письму с ответом не видно, доехал документ до модели или нет.
#
# текст хранится без обёртки, обёртку даёт ATTACHMENT_NOTE. те же слова
# работают вторым способом: когда до модели не дошло ни одного документа
# и ответа не будет, они уходят пользователю самостоятельным письмом
ATTACHMENT_NOTE = "\n\n(Примечание: {text})"
ATTACHMENT_SKIPPED_TEXT = "не удалось приложить к вопросу — {details}."


# результат сетевой фазы: документы, доехавшие до Open WebUI, и всё, что
# требуется сказать пользователю про остальные файлы письма
@dataclass
class UploadResult:
    # пары (проверенное вложение, идентификатор файла в Open WebUI)
    uploaded: List[Tuple[Any, str]] = field(default_factory=list)
    # тексты про файлы, не попавшие в запрос по дефекту файла либо по сбою сети
    skipped: List[str] = field(default_factory=list)

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


# вход: строки таблицы session_files, полученные storage.get_session_files.
# выход: пара (ссылки для запроса, описания документов).
# документ из первого письма обсуждается и в пятом, поэтому ссылки на живые
# файлы сессии уходят в каждый запрос. содержимое документа проект не хранит:
# из базы читаются имя файла и его вес
def _describe_stored(rows: Sequence) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Собирает ссылки и описания для файлов прежних писем треда."""
    from src import owui_files
    from src.attachments import size_words

    links, notes = [], []
    for row in rows:
        links.append(owui_files.reference(row["file_id"]))

        # нулевой вес стоит у файлов, записанных до перехода на серверный
        # разбор: size_words отдаёт для него «объём неизвестен»
        size = size_words(row["bytes"])
        notes.append(
            f"Ранее в переписке приложен документ «{row['filename']}» ({size}), "
            "доступен поиском по содержимому."
        )
    return links, notes


# вход: объект Attachment из email_parser.extract_attachments.
# выход: пара (проверенное вложение, идентификатор файла в Open WebUI).
# поднимает AttachmentError с причиной, пригодной для показа пользователю.
# побочный эффект: файл появляется в хранилище Open WebUI.
# строка в таблицу session_files здесь не пишется: запись идёт в build_context
# под замком сессии
def _upload_one(attachment) -> Tuple[Any, str]:
    """Проверяет вложение и кладёт его файл в Open WebUI."""
    from src import attachments, owui_files

    checked = attachments.check(attachment)

    # имя приводится к безопасному виду: оно уходит в общее хранилище.
    # расширение сохраняется — по нему Open WebUI выбирает парсер
    file_id = owui_files.upload(
        checked.upload_name, checked.payload, checked.upload_type
    )

    try:
        owui_files.wait_processed(file_id)

    # ветка неудачной обработки: недообработанный файл на вопросы не отвечает,
    # занимает место в хранилище и попадает под те же глаза, что и рабочие.
    # строки в session_files для него ещё нет (она пишется в build_context
    # под замком сессии) — файл никогда не станет учтённым документом
    # пользователя. уборка идёт с force=True: ATTACHMENT_DELETE_ENABLED задаёт
    # политику хранения документов, уже привязанных к сессии пользователя.
    # без force при выключенном флаге такой файл было бы нечем убрать даже
    # вручную: следующая уборка по сроку и reconcile --delete-orphans читают
    # ту же таблицу session_files, где строки для него нет
    except Exception:
        owui_files.delete(file_id, force=True)
        raise

    return checked, file_id


# вход: разобранное письмо и режим примерки.
# выход: UploadResult с загруженными документами и текстами примечаний.
# побочные эффекты: сетевые запросы к Open WebUI, до
# ATTACHMENT_PROCESS_TIMEOUT_SEC секунд ожидания на файл.
# фаза выполняется без замка сессии: письма одного треда ждали бы здесь друг
# друга по несколько минут, не имея к документам отношения.
# сбой на одном файле ответ не отменяет: пользователь получает ответ
# по остальному письму и примечание о том, что приложить не удалось
def upload_attachments(incoming: IncomingEmail, dry_run: bool) -> UploadResult:
    """Проверяет вложения письма и загружает их в Open WebUI."""
    from src.attachments import AttachmentError

    result = UploadResult()

    # ветка отключённой поддержки вложений: файлы остаются без обработки,
    # пользователь получает примечание
    if not ATTACHMENTS_ENABLED:
        if incoming.attachments:
            result.skipped.append("работа с вложениями отключена администратором")
        return result

    # имена файлов, уехавших с этим письмом на прошлой попытке: повторная
    # загрузка после сбоя отправки создала бы их копии в общем хранилище.
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

        # ветка дефекта файла: формат, вес, пустое содержимое
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

    for attachment, file_id in upload.uploaded:
        # примерка строк в базу не пишет; сами файлы снимает pipeline
        # вызовом drop_uploaded
        if not dry_run:
            _remember(attachment, file_id, session_id, incoming.message_id)

        context.files.append(owui_files.reference(file_id))
        descriptions.append(attachments.describe(attachment))

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

    if upload.skipped:
        context.notes.append(ATTACHMENT_SKIPPED_TEXT.format(details="; ".join(upload.skipped)))

    return context


# вход: проверенное вложение, идентификатор файла, сессия и Message-ID письма.
# побочный эффект: строка в таблице session_files.
# сбой записи снимает файл в Open WebUI перед подъёмом исключения: файл без
# строки в базе остаётся в общем хранилище навсегда — уборка по сроку хранения
# и команда forget работают по строкам таблицы.
# удаление идёт с force=True по той же причине, что и в _upload_one: строки
# в базе для этого файла нет и не будет — запись только что сорвалась,
# поэтому ATTACHMENT_DELETE_ENABLED (политика хранения документов пользователя)
# к нему не относится
def _remember(attachment, file_id: str, session_id: int, message_id: str) -> None:
    """Записывает загруженный файл за сессией."""
    from src import owui_files

    try:
        storage.add_session_file(
            session_id, file_id, attachment.filename, attachment.size_bytes, message_id,
        )
    except Exception:
        log.error(
            "файл %s загружен в Open WebUI, но не записан в базу — удаляю его",
            file_id, exc_info=True,
        )
        owui_files.delete(file_id, force=True)
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
