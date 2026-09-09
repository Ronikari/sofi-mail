# тесты обработки письма целиком: сопоставление сессий, идемпотентность, фильтры.
# порядок: make_email собирает MIME-сообщение -> pipeline.process_email проходит
# полный путь письма -> утверждение проверяет базу и список отправленных писем.
# вход: фикстуры allow_sender, allow_domain, fake_llm, sent_mail, transport
# из conftest.py и .eml-файлы из tests/fixtures.
# выход: результат pytest.
# проверяются pipeline.py и storage.py; сеть заменена заглушками conftest.py.
# запуск: pytest tests/test_pipeline.py

import email
from email.message import EmailMessage

from src import pipeline, storage
from src.email_parser import REPLY_MARKER

FROM = "Андрей <a.ludkov29@gmail.com>"
TO = "llm.assistant@gmail.com"


# вход: тема, Message-ID, тело письма, заголовки треда и адрес отправителя.
# выход: объект email.message.Message, готовый для pipeline.process_email
def make_email(subject, message_id, body="Вопрос?", in_reply_to=None, references=None, sender=FROM):
    """Собирает входящее письмо для теста."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = TO
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Sat, 25 Jul 2026 19:12:03 +0300"

    # заголовки треда ставятся по требованию теста: первое письмо сессии их
    # не несёт
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)

    msg.set_content(body, charset="utf-8")

    # письмо пересобирается из байтов: pipeline получает его в том же виде,
    # в каком отдаёт ews_client
    return email.message_from_bytes(msg.as_bytes())


# вход: тема, Message-ID, имя и содержимое приложенного документа, текст
# письма и заголовок треда.
# выход: письмо с одним вложением, пригодным для attachments.parse.
# расширение .txt разбирается тем же путём, что и остальные офисные форматы
def make_email_with_doc(
    subject, message_id, filename="документ.txt", text="Текст документа.",
    body="Вопрос по документу?", in_reply_to=None,
):
    """Собирает письмо с приложенным текстовым документом."""
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = TO
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Sat, 25 Jul 2026 19:12:03 +0300"

    # заголовок треда ставится по требованию теста: письмо продолжает сессию
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to

    msg.set_content(body, charset="utf-8")

    # add_attachment ставит Content-Disposition: attachment и имя файла,
    # по которым email_parser.extract_attachments отбирает вложения
    msg.add_attachment(
        text.encode("utf-8"), maintype="text", subtype="plain", filename=filename
    )
    return email.message_from_bytes(msg.as_bytes())


# --- сопоставление сессий ---------------------------------------------------


def test_reply_continues_session(allow_sender, fake_llm, sent_mail):
    """Ответ на письмо модели попадает в ту же сессию."""
    pipeline.process_email(make_email("Вопрос про Python", "<u1@mail>"))

    # ответ пользователя ссылается на Message-ID письма модели
    reply_to = sent_mail[0]["message_id"]

    pipeline.process_email(
        make_email("Re: Вопрос про Python", "<u2@mail>", "А подробнее?", in_reply_to=reply_to)
    )

    assert len(storage.list_sessions()) == 1

    # четыре реплики подряд означают, что второе письмо продолжило сессию
    history = storage.get_history(1, 40)
    assert [row["role"] for row in history] == ["user", "assistant", "user", "assistant"]


def test_quote_carries_the_whole_thread_to_the_user(allow_sender, fake_llm, sent_mail):
    """В цитату ответа уходит письмо целиком, вместе с накопленным тредом."""
    # пользователь видит в треде Outlook и свои прежние вопросы, и ответы
    # модели на них: клиент накапливает переписку в теле письма, а мы
    # цитируем это тело как есть
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))

    # второе письмо пользователя приходит с цитатой прошлой переписки —
    # так его собирает почтовый клиент
    accumulated = (
        "Второй вопрос\n\n"
        "От: Sofi <llm@company.ru>\n"
        "Тема: Тема\n\n"
        f"{REPLY_MARKER} ответ на: Первый вопрос\n\n"
        "Первый вопрос"
    )
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", accumulated, in_reply_to=sent_mail[0]["message_id"])
    )

    quoted = sent_mail[1]["quoted_body"]

    assert "Второй вопрос" in quoted
    assert "Первый вопрос" in quoted, "прежний вопрос пользователя пропал из треда"
    assert "ответ на: Первый вопрос" in quoted, "прежний ответ модели пропал из треда"

    # в модель при этом уходит только новый текст: цитату снимает email_parser,
    # иначе переписка дублировалась бы в каждой реплике истории
    assert fake_llm[1]["prompt"] == "Второй вопрос"


def test_history_reaches_the_model(allow_sender, fake_llm, sent_mail):
    """История прошлых реплик доходит до генерации ответа."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    # второй вызов модели: в prompt стоит новый вопрос, в history — прошлая пара
    second_call = fake_llm[1]
    assert second_call["prompt"] == "Второй вопрос"
    # реплика модели лежит в истории с меткой [Sofi]: pipeline ставит её
    # до записи, и модель отличает свою прежнюю реплику от реплики пользователя
    assert [row["body"] for row in second_call["history"]] == [
        "Первый вопрос",
        f"{REPLY_MARKER} ответ на: Первый вопрос",
    ]


def test_new_subject_starts_new_session(allow_sender, fake_llm, sent_mail):
    """Письмо с новой темой открывает отдельную сессию."""
    pipeline.process_email(make_email("Первая тема", "<u1@mail>"))
    pipeline.process_email(make_email("Вторая тема", "<u2@mail>"))

    assert len(storage.list_sessions()) == 2
    assert fake_llm[1]["history"] == [], "новая тема не должна тянуть чужой контекст"


def test_session_found_via_references_when_in_reply_to_lost(allow_sender, fake_llm, sent_mail):
    """Сессия находится по цепочке References без заголовка In-Reply-To."""
    pipeline.process_email(make_email("Тема", "<u1@mail>"))
    sent_id = sent_mail[0]["message_id"]

    # часть почтовых клиентов теряет In-Reply-To и сохраняет References
    pipeline.process_email(make_email("Re: Тема", "<u2@mail>", references=["<u1@mail>", sent_id]))

    assert len(storage.list_sessions()) == 1


def test_thread_headers_do_not_open_someone_elses_session(allow_domain, fake_llm, sent_mail):
    """Письмо с чужого адреса не продолжает сессию по заголовкам треда."""
    # сценарий: руководитель пересылает ответ модели коллеге, тот отвечает всем.
    # в письме коллеги стоит In-Reply-To на письмо модели, переписка при этом
    # принадлежит руководителю.
    # доменный whitelist здесь образует условие сценария: он делает коллегу
    # разрешённым отправителем
    secret = "Готовим сокращение отдела продаж"
    pipeline.process_email(
        make_email("Кадры", "<boss@company.ru>", secret, sender="boss@company.ru")
    )
    reply_to_boss = sent_mail[0]["message_id"]

    pipeline.process_email(
        make_email(
            "Re: Кадры", "<colleague@company.ru>", "О чём речь?",
            in_reply_to=reply_to_boss, sender="colleague@company.ru",
        )
    )

    assert sent_mail[1]["to"] == "colleague@company.ru"
    assert fake_llm[1]["history"] == [], "чужая переписка не должна попадать в контекст"
    assert len(storage.list_sessions()) == 2, "письму с другого адреса нужна своя сессия"


def test_reply_keeps_thread_headers(allow_sender, fake_llm, sent_mail):
    """Ответ несёт заголовки треда входящего письма."""
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    # без этих заголовков ответ уедет отдельным тредом у получателя
    assert sent_mail[0]["in_reply_to"] == "<u1@mail>"
    assert sent_mail[0]["to"] == "a.ludkov29@gmail.com"


# --- идемпотентность --------------------------------------------------------


def test_same_email_is_answered_once(allow_sender, fake_llm, sent_mail):
    """Повторная обработка того же письма ответа не даёт."""
    msg = make_email("Тема", "<u1@mail>")
    first = pipeline.process_email(msg)
    second = pipeline.process_email(msg)

    assert first.status == "ok"
    assert second.status == "skipped"
    assert len(sent_mail) == 1


def test_send_failure_returns_email_to_queue(allow_sender, fake_llm, transport, monkeypatch):
    """Сбой отправки возвращает письмо в очередь следующего прохода."""
    transport.send_error = OSError("EWS недоступен")

    # пауза _retry гасится: три попытки заняли бы 6 секунд теста
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"))

    assert outcome.status == "error"
    # заявка снята: повторная обработка того же письма разрешена
    assert storage.claim_message("<u1@mail>") is True


def test_retry_after_send_failure_does_not_duplicate_question(
    allow_sender, fake_llm, transport, monkeypatch
):
    """Повторный проход не кладёт второй экземпляр вопроса в историю."""
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    # первый проход обрывается на отправке
    transport.send_error = OSError("нет сети")
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    # второй проход идёт по тому же письму, отправка работает
    transport.send_error = None
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    roles = [row["role"] for row in storage.get_history(1, 40)]
    assert roles == ["user", "assistant"]


def test_retry_after_send_failure_stays_in_one_session(
    allow_sender, fake_llm, transport, monkeypatch
):
    """Повтор после сбоя отправки остаётся в своей сессии."""
    # реплика с вопросом уже лежит в сессии, колонка messages.message_id
    # уникальна на всю базу: новая сессия потеряла бы вопрос на INSERT OR IGNORE,
    # и ответ лёг бы в неё отдельно от вопроса — тред разошёлся бы на вопрос
    # без ответа и ответ без вопроса.
    # склейка по теме выключена фикстурой thread_matching, поэтому проверяется
    # возврат письма в свою сессию по собственному Message-ID
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)
    transport.send_error = OSError("нет сети")
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    transport.send_error = None
    pipeline.process_email(make_email("Тема", "<u1@mail>"))

    sessions = storage.list_sessions()
    assert len(sessions) == 1, "повтор открыл вторую сессию вместо своей"
    assert [row["role"] for row in storage.get_history(sessions[0]["id"], 40)] == [
        "user",
        "assistant",
    ]


def test_llm_failure_is_reported_and_recorded(allow_sender, sent_mail, monkeypatch):
    """Отказ модели уходит пользователю письмом и попадает в журнал ошибок."""
    from src import llm

    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    # генератор внутри throw поднимает исключение при каждом вызове generate
    monkeypatch.setattr(llm, "generate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vLLM недоступен")))

    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"))

    assert outcome.status == "error"
    assert not outcome.can_mark_seen, "письмо с ошибкой должно остаться видимым для retry"
    assert "vLLM недоступен" in sent_mail[0]["body"]
    assert [row["message_id"] for row in storage.list_failed()] == ["<u1@mail>"]


# --- фильтры ----------------------------------------------------------------


def test_stranger_is_ignored_silently(allow_sender, fake_llm, sent_mail):
    """Письмо от адреса вне whitelist остаётся без ответа."""
    outcome = pipeline.process_email(make_email("Тема", "<x@mail>", sender="spam@evil.com"))

    assert outcome.status == "skipped"
    assert sent_mail == [], "посторонним не отвечаем: ответ подтвердил бы, что ящик живой"


def test_own_email_is_ignored(allow_sender, fake_llm, sent_mail):
    """Письмо с адреса самого ящика остаётся без ответа."""
    outcome = pipeline.process_email(make_email("Тема", "<x@mail>", sender=TO))

    assert outcome.status == "skipped"
    assert sent_mail == []


def test_rate_limit_stops_answering(allow_sender, fake_llm, sent_mail, monkeypatch):
    """Письма сверх часового лимита получают уведомление о лимите."""
    monkeypatch.setattr(pipeline, "RATE_LIMIT_PER_HOUR", 2)

    for i in range(4):
        pipeline.process_email(make_email(f"Тема {i}", f"<u{i}@mail>"))

    # ответы модели отличаются от уведомлений по началу текста; ответ помечен
    # в pipeline, уведомление — только при сборке письма в reply_builder
    answers = [mail for mail in sent_mail if mail["body"].startswith(f"{REPLY_MARKER} ответ на:")]
    assert len(answers) == 2
    assert "Превышен лимит" in sent_mail[2]["body"]


def test_long_email_is_truncated_not_rejected(allow_sender, fake_llm, sent_mail, monkeypatch):
    """Длинное письмо обрезается по лимиту и получает ответ с примечанием."""
    monkeypatch.setattr(pipeline, "MAX_PROMPT_CHARS", 100)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "я" * 500))

    assert len(fake_llm[0]["prompt"]) == 100
    assert "обработано частично" in sent_mail[0]["body"]


def test_dry_run_has_no_side_effects(allow_sender, fake_llm, sent_mail):
    """Режим примерки не меняет ни ящик, ни базу."""
    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"), dry_run=True)

    assert outcome.status == "ok"
    assert sent_mail == []
    assert storage.list_sessions() == []
    # письмо не съедено: обычный запуск обработает его как новое
    assert storage.claim_message("<u1@mail>") is True


def test_tnef_email_gets_format_hint_not_empty_body_hint(allow_domain, fake_llm, sent_mail):
    """Письмо в формате RTF получает подсказку про формат письма."""
    # подсказка «в письме не нашлось текста» направила бы пользователя искать
    # ошибку в своём тексте: текст он написал, до сервиса он не дошёл
    # из-за контейнера winmail.dat
    import email as email_module
    from pathlib import Path

    raw = (Path(__file__).parent / "fixtures" / "outlook_tnef.eml").read_bytes()
    outcome = pipeline.process_email(email_module.message_from_bytes(raw))

    assert outcome.status == "skipped"
    assert "winmail.dat" in sent_mail[0]["body"]
    assert fake_llm == [], "модель не должна вызываться для нечитаемого письма"


def test_forwarded_email_without_own_text_still_answers_from_context(
    allow_sender, fake_llm, sent_mail
):
    """Пересылка без слов от себя всё равно уходит в модель как контекст."""
    # шапка «От:/Кому:/Тема:» вырезается, остальной текст пересылки идёт
    # в запрос: для сессии этого отправителя пересланный тред не дублирует
    # историю, письмо новое
    msg = make_email(
        "Fwd: Кадры", "<f1@mail>",
        body="От: boss@company.ru\nКому: dept@company.ru\nТема: Кадры\n\nГотовим сокращение",
    )

    outcome = pipeline.process_email(msg)

    assert outcome.status == "ok"
    assert fake_llm and "сокращение" in fake_llm[0]["prompt"]
    assert storage.get_history(1, 40) != [], "пересланный текст должен лечь в историю сессии"


def test_forwarded_conversation_with_model_reaches_the_model(allow_sender, fake_llm, sent_mail):
    """Переписка с моделью, пересланная другим пользователем, доходит до модели."""
    # второй пользователь получил переписку с моделью пересылкой и переслал
    # её в ящик модели: маркер REPLY_MARKER внутри пересланного текста
    # не должен обрезать письмо — для сессии второго пользователя эта
    # переписка не лежит в истории и служит контекстом
    body = (
        "---------- Forwarded message ---------\n"
        "От: boss@company.ru\nКому: dept@company.ru\nТема: Кадры\n\n"
        "Готовим сокращение отдела продаж\n\n"
        f"{REPLY_MARKER} Сокращение затронет три позиции.\n"
        f"{REPLY_MARKER} · сессия «Кадры»\n"
    )
    msg = make_email("Fwd: Кадры", "<f2@mail>", body=body)

    outcome = pipeline.process_email(msg)

    assert outcome.status == "ok"
    assert fake_llm
    assert "три позиции" in fake_llm[0]["prompt"]


def test_question_above_forwarded_conversation_keeps_the_thread(
    allow_sender, fake_llm, sent_mail
):
    """Вопрос над пересылкой уходит в модель вместе с самим тредом."""
    # второй пользователь спрашивает своими словами прямо над разделителем
    # "---------- Forwarded message ---------": разделитель раньше читался
    # как граница цитаты, и весь тред ниже него в модель не попадал —
    # вопрос доходил без предмета, о котором спрашивают
    body = (
        "Расскажи, что в этой переписке\n\n"
        "---------- Forwarded message ---------\n"
        "От: boss@company.ru\nКому: dept@company.ru\nТема: Кадры\n\n"
        "Готовим сокращение отдела продаж\n\n"
        f"{REPLY_MARKER} Сокращение затронет три позиции.\n"
        f"{REPLY_MARKER} · сессия «Кадры»\n"
    )
    msg = make_email("Fwd: Кадры", "<f3@mail>", body=body)

    outcome = pipeline.process_email(msg)

    assert outcome.status == "ok"
    assert fake_llm
    prompt = fake_llm[0]["prompt"]
    assert "Расскажи, что в этой переписке" in prompt
    assert "три позиции" in prompt


# --- отправка и запись после неё --------------------------------------------


def test_send_is_attempted_once(allow_sender, fake_llm, transport, monkeypatch):
    """Отправка письма выполняется одной попыткой."""
    # доставка через Exchange не идемпотентна: повтор после неясного исхода
    # даёт получателю второе письмо с тем же ответом
    attempts = []

    def failing(**kwargs):
        attempts.append(kwargs)
        raise OSError("EWS недоступен")

    monkeypatch.setattr(transport, "send_reply", failing)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)

    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"))

    assert outcome.status == "error"
    assert len(attempts) == 1, "отправка повторялась"
    # заявка снята: письмо попадёт в следующий проход обычным путём
    assert storage.claim_message("<u1@mail>") is True


def test_history_write_failure_after_send_keeps_status_ok(
    allow_sender, fake_llm, sent_mail, monkeypatch
):
    """Сбой записи истории после отправки не отправляет письмо второй раз."""
    # перевод заявки в error отправил бы письмо в команду retry, та сняла бы
    # признак прочитанности, и следующий проход сгенерировал бы второй ответ
    # на тот же вопрос
    real_add = storage.add_message

    def failing(session_id, role, body, message_id, body_raw=""):
        if role == "assistant":
            raise RuntimeError("database is locked")
        return real_add(session_id, role, body, message_id, body_raw)

    monkeypatch.setattr(storage, "add_message", failing)

    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>"))

    assert outcome.status == "ok"
    assert "сбой записи" in outcome.detail
    assert len(sent_mail) == 1
    assert storage.list_failed() == [], "письмо не должно попадать в retry"
    # строка журнала на месте: повторная обработка того же письма запрещена
    assert storage.claim_message("<u1@mail>") is False


def test_upload_is_rolled_back_when_db_write_fails(
    allow_sender, fake_llm, fake_owui_files, monkeypatch
):
    """Сбой записи файла в базу снимает загруженный файл в Open WebUI."""
    # файл без строки в таблице session_files остаётся в общем хранилище
    # навсегда: уборка по сроку хранения и команда forget работают по строкам
    def failing(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(storage, "add_session_file", failing)

    outcome = pipeline.process_email(make_email_with_doc("Тема", "<d1@mail>"))

    assert outcome.status == "error"
    assert fake_owui_files.uploaded, "файл должен был загрузиться до сбоя"
    assert fake_owui_files.deleted == ["file-1"]
    assert fake_owui_files.alive == set(), "файл остался в хранилище без записи в базе"


def test_attachment_block_counts_toward_the_fold_threshold(
    allow_sender, fake_llm, fake_owui_files, sent_mail, monkeypatch
):
    """Блок описаний вложений учитывается в пороге свёртки сессии — регресс-тест.

    До правки summarizer.fit_session получал только текст письма, без
    attachments_ctx.prompt_prefix. Короткое письмо с объёмным вложением
    не запускало свёртку, хотя итоговый запрос (история + блок описаний +
    письмо) всё равно перерастал SESSION_MAX_CHARS на шаге llm.build_messages,
    а llm.warn_over_budget историю не режет — превышение уходило на сервер
    молча (тот же класс регресса, что уже чинили для тела письма, review.md п.6).
    """
    from src import summarizer

    pipeline.process_email(make_email("Тема", "<q1@mail>", "Q1"))

    history = storage.get_history(1)
    assert len(history) == 2, "нужны минимум две реплики — иначе over_limit не сработает"
    base_chars = summarizer.context_chars(history)

    # порог сразу над «история + короткое тело письма»: тело второго письма
    # само по себе свёртку не запускает, блок описания вложения (~80+ символов
    # шаблонного текста) — запускает
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", base_chars + len("Q2") + 10)

    pipeline.process_email(
        make_email_with_doc(
            "Re: Тема", "<q2@mail>", body="Q2", in_reply_to=sent_mail[0]["message_id"],
        )
    )

    folded = storage.get_history(1)
    assert folded[0]["role"] == storage.SUMMARY_ROLE, (
        "свёртка не сработала: блок описаний вложения не был учтён в пороге сессии"
    )
