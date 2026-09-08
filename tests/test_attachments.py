"""Вложения письма: что берём из MIME и что пропускаем в Open WebUI.

Сети здесь нет: и извлечение из MIME, и проверка вложения — чистые функции,
поэтому проверяются на письмах, собранных прямо в тесте.

Разбора документа в проекте больше нет — файл уходит в Open WebUI как есть,
текст из него извлекает сервер. Поэтому проверять здесь нечего, кроме границы:
что доезжает до сервера, под каким именем и с каким mime-типом.
"""

from email.message import EmailMessage

import pytest

from src import attachments
from src.attachments import Attachment, AttachmentError, check, safe_name, size_words
from src.email_parser import extract_attachments, parse_email


def make_email(*, attach=(), inline=(), body="Вопрос по документу."):
    msg = EmailMessage()
    msg["From"] = "Иванов <ivanov@company.ru>"
    msg["To"] = "sofi@company.ru"
    msg["Subject"] = "Документ"
    msg["Message-ID"] = "<doc-1@mail>"
    msg.set_content(body)
    for name, data in attach:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    for cid, data in inline:
        msg.add_attachment(data, maintype="image", subtype="png", filename=cid, cid=f"<{cid}>")
    return msg


# --- 1. Что считается вложением ---------------------------------------------


def test_user_attachment_is_taken():
    msg = make_email(attach=[("смета.xlsx", b"payload")])

    found = extract_attachments(msg)

    assert [a.filename for a in found] == ["смета.xlsx"]
    assert found[0].payload == b"payload"


def test_inline_image_of_signature_is_skipped():
    """Логотип из подписи приезжает вложением, но к вопросу отношения не имеет."""
    msg = make_email(attach=[("договор.docx", b"x")], inline=[("logo.png", b"\x89PNG")])

    assert [a.filename for a in extract_attachments(msg)] == ["договор.docx"]


def test_winmail_dat_is_not_an_attachment():
    """TNEF — это упаковка письма, про неё у пайплайна свой ответ."""
    msg = make_email(attach=[("winmail.dat", b"tnef")])

    assert extract_attachments(msg) == []


def test_attachments_reach_parsed_email():
    """Пайплайн берёт вложения из разобранного письма, а не лезет в MIME сам."""
    incoming = parse_email(make_email(attach=[("акт.pdf", b"%PDF-1.4")]))

    assert [a.filename for a in incoming.attachments] == ["акт.pdf"]


def test_order_of_attachments_is_preserved():
    """«По первому документу вопрос такой» — порядок несёт смысл."""
    msg = make_email(attach=[("первый.txt", b"a"), ("второй.txt", b"b")])

    assert [a.filename for a in extract_attachments(msg)] == ["первый.txt", "второй.txt"]


# --- 2. Отказы, у которых должно быть понятное объяснение --------------------


def test_archive_is_refused():
    """Внутри архива ещё один слой вложений — ответ про него отличается от ответа про формат."""
    with pytest.raises(AttachmentError, match="не поддерживается"):
        check(Attachment("архив.zip", "application/zip", b"PK\x03\x04"))


def test_file_without_extension_is_refused():
    """Open WebUI выбирает парсер по расширению: без него разбирать нечем."""
    with pytest.raises(AttachmentError, match="без расширения"):
        check(Attachment("dump", "application/octet-stream", b"data"))


def test_oversized_file_is_rejected(monkeypatch):
    monkeypatch.setattr(attachments, "ATTACHMENT_MAX_MB", 1)

    with pytest.raises(AttachmentError, match="больше 1 МБ"):
        check(Attachment("большой.txt", "text/plain", b"x" * 2 * 1024 * 1024))


def test_empty_file_is_refused():
    """Пустой файл сервер проиндексирует пустотой, и ответ по нему соберётся из ничего."""
    with pytest.raises(AttachmentError, match="пустой"):
        check(Attachment("пусто.txt", "text/plain", b""))


def test_safe_name_strips_paths():
    """Имя уезжает в чужое хранилище — путей и служебных символов в нём быть не должно."""
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("отчёт за 2026;rm -rf.txt") == "отчёт за 2026_rm -rf.txt"


def test_upload_name_keeps_the_extension():
    """По расширению Open WebUI выбирает парсер: подменять его нельзя."""
    assert Attachment("акт 2026.pdf", "application/pdf", b"%PDF").upload_name == "акт 2026.pdf"


# --- 3. Форматы, которые раньше отсекались локальным разбором ----------------


@pytest.mark.parametrize("filename", ["скан.png", "фото.jpeg", "отчёт.doc", "смета.xls"])
def test_formats_of_the_server_side_parser_pass(filename):
    """Картинки и Office 97-2003 разбирает сервер: локальных отказов по ним больше нет."""
    assert check(Attachment(filename, "application/octet-stream", b"data")).filename == filename


# --- 4. Mime-тип, с которым файл уходит в хранилище --------------------------


def test_declared_content_type_wins():
    """Тип из письма проставил почтовый клиент по содержимому — он точнее расширения."""
    assert Attachment("акт.pdf", "application/pdf; charset=binary", b"%PDF").upload_type == "application/pdf"


def test_octet_stream_is_refined_by_extension():
    """Outlook ставит octet-stream на любое вложение — уточняем по расширению."""
    guessed = Attachment("акт.pdf", "application/octet-stream", b"%PDF").upload_type

    assert guessed == "application/pdf"


def test_unknown_extension_falls_back_to_octet_stream():
    """Тип не определён ни письмом, ни расширением: разбору это не мешает."""
    probe = Attachment("данные.rst", "", b"text")

    assert probe.upload_type in ("text/x-rst", "text/prs.fallenstein.rst", attachments.DEFAULT_CONTENT_TYPE)


# --- 5. Как документ описывается модели и человеку ---------------------------


@pytest.mark.parametrize(
    "size, expected",
    [(0, "объём неизвестен"), (300, "1 КБ"), (412 * 1024, "412 КБ"), (3 * 1024 * 1024, "3.0 МБ")],
)
def test_size_words_reads_like_a_letter(size, expected):
    assert size_words(size) == expected


def test_description_names_the_document():
    """Вопрос «что в акте» без описания не связывается ни с одним из файлов."""
    described = attachments.describe(Attachment("акт.pdf", "application/pdf", b"x" * 2048))

    assert "«акт.pdf»" in described and "2 КБ" in described


def test_context_block_is_cut_to_the_limit():
    """Блок описаний входит в MAX_CONTEXT_CHARS и вытесняет историю сессии."""
    block = attachments.context_block(["a" * 100, "b" * 100], limit=50)

    assert len(block.strip()) == 50


def test_context_block_of_nothing_is_empty():
    assert attachments.context_block([]) == ""


def test_selftest_reports_the_limits():
    """check в cli падает, если проверка вложений не работает вовсе."""
    assert "МБ на файл" in attachments.selftest()
