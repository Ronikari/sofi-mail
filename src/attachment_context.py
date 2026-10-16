# подготовка вложений к запросу: upload_attachments грузит файлы без замка сессии,
# build_context пишет session_files и собирает описания под замком, без сети.

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

# примечания сообщают пользователю о проблеме с файлом; тем же текстом уходит отдельное
# письмо, если до модели не дошло ни одного документа.
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
    # подмножество skipped, отделённое от сбоев сети: pipeline.py по нему
    # останавливает обработку письма — вопрос задан по документу, которого у модели нет.
    rejected: List[str] = field(default_factory=list)

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

    # используется, когда ответа модели не будет вовсе — приписка в скобках
    # оказалась бы приписана к пустому месту.
    @property
    def standalone_notice(self) -> str:
        """Собирает примечания как текст отдельного письма."""
        return "\n\n".join(self.notes)


# документ из первого письма обсуждается и в пятом, поэтому ссылки на живые файлы сессии
# уходят в каждый запрос; содержимое проект не хранит — из базы читаются имя и вес.
def _describe_stored(rows: Sequence) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Собирает ссылки и описания для файлов прежних писем треда."""
    from src import owui_files
    from src.attachments import is_image_name, size_words

    links, notes = [], []
    for row in rows:
        image = is_image_name(row["filename"])
        links.append(owui_files.reference(row["file_id"], row["filename"], image=image))

        # нулевой вес стоит у файлов, записанных до перехода на серверный
        # разбор: size_words отдаёт для него «объём неизвестен»
        size = size_words(row["bytes"])

        # история сессии хранится текстом: картинка прежнего письма передаётся модели повторно
        if image:
            notes.append(
                f"Ранее в переписке приложено изображение «{row['filename']}» ({size}), "
                "оно повторно передано вместе с вопросом."
            )
            continue

        notes.append(
            f"Ранее в переписке приложен документ «{row['filename']}» ({size}), "
            "доступен поиском по содержимому."
        )
    return links, notes


# поднимает AttachmentError с причиной, пригодной для показа пользователю.
# строка в session_files здесь не пишется: запись идёт в build_context под замком сессии.
def _upload_one(attachment) -> Tuple[Any, str]:
    """Проверяет вложение и кладёт его файл в Open WebUI."""
    from src import attachments, owui_files

    checked = attachments.check(attachment)

    # имя приводится к безопасному виду: оно уходит в общее хранилище.
    # расширение сохраняется — по нему Open WebUI выбирает парсер.
    # разбор image/* в Open WebUI 0.8.12 ставит файлу статус failed
    file_id = owui_files.upload(
        checked.upload_name, checked.payload, checked.upload_type,
        process=not checked.is_image,
    )

    # у файла без разбора статуса нет: эндпоинт отдаёт pending до таймаута
    if checked.is_image:
        return checked, file_id

    try:
        owui_files.wait_processed(file_id)

    # force=True: строки в session_files для этого файла нет и не будет, обычная уборка
    # по ATTACHMENT_DELETE_ENABLED его никогда не найдёт.
    except Exception:
        owui_files.delete(file_id, force=True)
        raise

    return checked, file_id


# без замка сессии — письма треда иначе ждали бы друг друга минуты на загрузке файлов.
# сбой одного файла не останавливает остальные — решение по skipped/rejected за pipeline.py.
def upload_attachments(incoming: IncomingEmail, dry_run: bool) -> UploadResult:
    """Проверяет вложения письма и загружает их в Open WebUI."""
    from src.attachments import AttachmentError
    from src.owui_files import FileRejected

    result = UploadResult()

    # ветка отключённой поддержки вложений: файлы остаются без обработки,
    # пользователь получает примечание
    if not ATTACHMENTS_ENABLED:
        if incoming.attachments:
            result.skipped.append("работа с вложениями отключена администратором")
        return result

    # имена файлов, уехавших с этим письмом на прошлой попытке — повторная загрузка
    # после сбоя отправки создала бы их копии; замок не нужен, чтение идёт по message_id.
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

        # ветка дефекта файла: причина попадает и в skipped, и в rejected —
        # по rejected pipeline.py останавливает обработку письма.
        except AttachmentError as exc:
            log.warning("вложение %s пропущено: %s", attachment.filename, exc)
            reason = f"«{attachment.filename}»: {exc}"
            result.skipped.append(reason)
            result.rejected.append(reason)

        # ловится до общего Exception — FileRejected наследует FileError,
        # порядок веток здесь определяет исход.
        except FileRejected as exc:
            log.warning("вложение %s отклонено Open WebUI: %s", attachment.filename, exc)
            reason = f"«{attachment.filename}»: отклонено сервисом документов — {exc}"
            result.skipped.append(reason)
            result.rejected.append(reason)

        # пользователю называется сервис, текст исключения остаётся в логе.
        except Exception as exc:
            log.error("вложение %s не загружено: %s", attachment.filename, exc)
            result.skipped.append(f"«{attachment.filename}»: сервис документов недоступен")

    return result


# вызывается под замком сессии, чтобы письма одного треда писали свои файлы по очереди
# и каждое видело согласованный список прежних файлов.
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

        context.files.append(
            owui_files.reference(file_id, attachment.filename, image=attachment.is_image)
        )
        descriptions.append(attachments.describe(attachment))

    # идентификаторы этого письма исключаются из выборки прежних файлов, иначе
    # файл попал бы в запрос дважды — ссылкой и описанием «ранее приложен».
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

    # длину блока ограничивает MAX_ATTACHMENT_CONTEXT_CHARS: вместе с письмом он входит
    # в MAX_CONTEXT_CHARS и вытесняет историю сессии.
    context.prompt_prefix = context_block(descriptions)

    if upload.skipped:
        context.notes.append(ATTACHMENT_SKIPPED_TEXT.format(details="; ".join(upload.skipped)))

    return context


# сбой записи в базу снимает файл в Open WebUI с force=True — без строки в session_files
# обычная уборка (по сроку, forget) его не найдёт никогда.
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


# force=True: примерка не пишет session_files, без него выключенная ATTACHMENT_DELETE_ENABLED
# оставляла бы файл в Open WebUI навсегда — по десятку копий на `once --dry-run`.
def drop_uploaded(file_ids: Sequence[str]) -> None:
    """Удаляет из Open WebUI файлы, загруженные в режиме примерки."""
    if not file_ids:
        return

    from src import owui_files

    for file_id in file_ids:
        owui_files.delete(file_id, force=True)
