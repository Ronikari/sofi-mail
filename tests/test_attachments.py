"""Вложения письма: что берём из MIME и что пропускаем в Open WebUI — чистыми функциями, без сети.
Разбора документа в проекте больше нет: важна только граница — имя и mime-тип файла на сервер."""

import email
from email.message import EmailMessage
from pathlib import Path

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
    # картинки inline стоят в автоподписи Outlook для Windows
    if inline:
        refs = "".join(f'<img src="cid:{cid}">' for cid, _ in inline)
        msg.add_alternative(f'<p>{body}</p><a name="_MailAutoSig"></a><p>{refs}</p>', subtype="html")
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


def test_attached_image_with_content_id_is_taken():
    """Картинка с Content-ID без ссылки cid: из html считается вложением пользователя."""
    msg = make_email(inline=[("logo.png", b"\x89PNG")])
    msg.add_attachment(
        b"\x89PNG-screen", maintype="image", subtype="png",
        filename="screen.png", cid="<F3A1B2C4@EURPRD01.prod.outlook.com>",
    )

    found = extract_attachments(msg)

    assert [a.filename for a in found] == ["screen.png"]
    assert found[0].is_image


def test_inline_image_referenced_by_encoded_cid_is_skipped():
    """Ссылка cid: в html бывает url-кодированной: @ приходит как %40."""
    msg = EmailMessage()
    msg["From"] = "ivanov@company.ru"
    msg["Subject"] = "Документ"
    msg.set_content("Вопрос.")
    msg.add_alternative('<div id="Signature"><img src="cid:image001.png%4001DA0000"></div>', subtype="html")
    msg.add_attachment(
        b"\x89PNG", maintype="image", subtype="png",
        filename="image001.png", cid="<image001.png@01DA0000>",
    )

    assert extract_attachments(msg) == []


def outlook_email(html, images):
    """Собирает письмо Outlook: html-тело и картинки, на которые оно ссылается по cid:."""
    msg = EmailMessage()
    msg["From"] = "ivanov@company.ru"
    msg["Subject"] = "Ошибка"
    msg.set_content("текстовая часть")
    msg.add_alternative(html, subtype="html")
    related = msg.get_payload()[1]
    for cid, filename in images:
        related.add_related(
            f"{cid}-bytes".encode(), maintype="image", subtype="png",
            cid=f"<{cid}>", filename=filename,
        )
    return msg


def test_image_pasted_above_outlook_signature_is_taken():
    """Скриншот над автоподписью уходит модели, логотип подписи и картинка цитаты — нет."""
    msg = outlook_email(
        '<div class=WordSection1><p>Вот ошибка:</p><p><img src="cid:image001.png@01DA"></p>'
        '<p><a name="_MailAutoSig">С уважением, Иванов</a></p><p><img src="cid:image002.png@01DA"></p>'
        '<div id="divRplyFwdMsg"><b>От:</b> Петров</div><p><img src="cid:image003.png@01DA"></p></div>',
        [("image001.png@01DA", "image001.png"), ("image002.png@01DA", "image002.png"),
         ("image003.png@01DA", "image003.png")],
    )

    found = extract_attachments(msg)

    assert [a.filename for a in found] == ["image001.png"]
    assert found[0].inline and found[0].is_image
    assert "вставлено изображение «image001.png»" in attachments.describe(found[0])


@pytest.mark.parametrize("signature", [
    '<div id="Signature"><img src="cid:logo"></div>',                # OWA, новый Outlook
    '<div id="ms-outlook-mobile-signature"><img src="cid:logo"></div>',
    '<div class="gmail_signature"><img src="cid:logo"></div>',
    '<p>-- </p><p>Иванов</p><img src="cid:logo">',                    # разделитель RFC 3676
    '<p><i>С уважением,<br><b>Иванов</b></i></p><img src="cid:logo">',  # подпись без разметки
])
def test_signature_image_is_skipped(signature):
    """Картинка блока подписи вложением не считается, картинка над подписью — считается."""
    msg = outlook_email(
        f'<p>Что на скриншоте?</p><img src="cid:shot"><br>{signature}',
        [("shot", "shot.png"), ("logo", "logo.png")],
    )

    assert [a.filename for a in extract_attachments(msg)] == ["shot.png"]


def test_corporate_outlook_letter_keeps_screenshot_and_drops_logo():
    """Письмо Outlook корпоративного контура: автоподпись без закладки _MailAutoSig, логотип в конце."""
    msg = email.message_from_bytes(
        (Path(__file__).parent / "fixtures" / "outlook_corporate_signature.eml").read_bytes()
    )

    found = extract_attachments(msg)

    assert [(a.filename, a.inline) for a in found] == [("image001.png", True)]


def test_image_in_quote_of_reply_is_skipped():
    """Картинка прежнего письма в цитате отсекается по шапке «От:/Отправлено:»."""
    msg = outlook_email(
        '<p>А это что?</p><img src="cid:new"><p><b>От:</b> Петров<br><b>Отправлено:</b> 1 октября'
        '<br><b>Кому:</b> Иванов<br><b>Тема:</b> Ошибка</p><img src="cid:old">',
        [("new", "new.png"), ("old", "old.png")],
    )

    assert [a.filename for a in extract_attachments(msg)] == ["new.png"]


def test_pasted_image_without_filename_gets_a_name():
    """Apple Mail не даёт имени картинке в тексте: имя строится по mime-типу."""
    msg = outlook_email('<p>Смотри</p><img src="cid:shot">', [("shot", None)])

    found = extract_attachments(msg)

    assert [a.filename for a in found] == ["image001.png"]
    assert found[0].inline


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
