"""Утечка контекста сессии через цитату предыдущих писем.

Баг, найденный в опытной эксплуатации: начиная со второго-третьего письма
в треде модель отвечала так, будто истории нет. Цепочка была такой:

1. почтовый клиент вставлял шапку цитаты в форме, которую отсечение не узнавало
   (шапка начинается не с «От:», приклеена к тексту без перевода строки, либо
   пользователь дописал ответ под цитатой);
2. `parse_email` откатывался на неочищенное тело — в промпт и в историю сессии
   уезжал весь тред целиком вместе с шапками;
3. на следующем письме раздутая реплика не влезала в окно модели, и сборка
   контекста выбрасывала всю историю сессии, оставляя один последний вопрос.

Закрыто с обоих концов. Реплика сессии содержит только новый текст письма:
служебные блоки «От:/Отправлено:/Кому:/Тема:», которые ставит почтовый сервер,
служат границей цитаты, а прежние ответы модели опознаются по метке [Sofi]
в первой строке (`strip_own_replies`). Контекст сессии — одна цепочка реплик,
и растёт она линейно. Отбрасывания истории при сборке запроса больше нет:
`llm.warn_over_budget` отдаёт её целиком, а объёмом сессии управляет
`summarizer.fit_session` заранее, ещё до этого шага.

Тесты идут по всем трём звеньям: разбор письма, сборка контекста и пайплайн
целиком на цепочке из четырёх писем.
"""

import email
from email.message import EmailMessage

import pytest

from src import llm, pipeline, storage
from src.email_parser import (
    REPLY_MARKER,
    html_to_text,
    parse_email,
    strip_header_blocks,
    strip_own_replies,
    strip_quoted,
)

FROM = "Андрей <a.ludkov29@gmail.com>"
TO = "llm.assistant@gmail.com"

QUESTION = "Давай пример кода."

# признаки, которых в теле письма быть не должно: сама шапка и текст из цитаты
LEAK_MARKERS = ("Отправлено:", "Sent:", "Кому:", "Тема:", "llm@company.ru", "Есть три способа")


def as_message(body: str, subtype: str = "plain") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = TO
    msg["Subject"] = "RE: Вопрос про Python"
    msg["Message-ID"] = "<quoted@outlook.com>"
    msg["Date"] = "Tue, 18 Aug 2026 10:20:00 +0300"
    msg.set_content(body, subtype=subtype, charset="utf-8")
    return email.message_from_bytes(msg.as_bytes())


# --- 1. Разбор письма: шапка цитаты не должна попадать в тело ----------------

OUTLOOK_PLAIN_RU = QUESTION + """

От: Local LLM <llm@company.ru>
Отправлено: понедельник, 18 августа 2026 г. 10:15
Кому: Иванов Иван <ivanov@company.ru>
Тема: Re: Вопрос про Python

Есть три способа отсортировать список.
"""

OUTLOOK_PLAIN_EN = QUESTION + """

From: Local LLM <llm@company.ru>
Sent: Monday, August 18, 2026 10:15 AM
To: Ivanov Ivan <ivanov@company.ru>
Subject: Re: Python question

Есть три способа отсортировать список.
"""

# шлюзы и часть клиентов переставляют поля местами: первой идёт «Тема:»,
# а построчная эвристика ждала «От:» и всю шапку пропускала
BLOCK_STARTS_WITH_SUBJECT = QUESTION + """

Тема: Re: Вопрос про Python
От: Local LLM <llm@company.ru>
Кому: Иванов Иван <ivanov@company.ru>

Есть три способа отсортировать список.
"""

# OWA и новый Outlook: цитата в divRplyFwdMsg
OWA_HTML = html_to_text(
    """<html><body>
<div dir="ltr">Давай пример кода.</div>
<div id="appendonsend"></div>
<hr style="display:inline-block;width:98%">
<div id="divRplyFwdMsg" dir="ltr">
<b>От:</b> Local LLM &lt;llm@company.ru&gt;<br>
<b>Отправлено:</b> 18 августа 2026 г. 10:15<br>
<b>Кому:</b> Иванов Иван &lt;ivanov@company.ru&gt;<br>
<b>Тема:</b> Re: Вопрос про Python</div>
<div>Есть три способа отсортировать список.</div>
</body></html>"""
)

# Outlook desktop: цитата не в blockquote, а в div с рамкой сверху
OUTLOOK_DESKTOP_HTML = html_to_text(
    """<html><body><div class=WordSection1>
<p class=MsoNormal>Давай пример кода.<o:p></o:p></p>
<div><div style='border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm'>
<p class=MsoNormal><b>От:</b> Local LLM &lt;llm@company.ru&gt; <br>
<b>Отправлено:</b> 18 августа 2026 г. 10:15<br>
<b>Кому:</b> Иванов Иван<br>
<b>Тема:</b> Re: Вопрос про Python<o:p></o:p></p></div></div>
<p class=MsoNormal>Есть три способа отсортировать список.<o:p></o:p></p>
</div></body></html>"""
)

# клиент разделил текст и шапку <span>, а не <br>: после снятия тегов шапка
# оказывается приклеенной к последней строке пользователя
INLINE_HTML = html_to_text(
    """<html><body>
<div>Давай пример кода.<span><b>От:</b> Local LLM &lt;llm@company.ru&gt;
<b>Отправлено:</b> 18 августа 2026 г. 10:15 <b>Кому:</b> Иванов Иван <b>Тема:</b> Re: Вопрос</span></div>
<div>Есть три способа отсортировать список.</div>
</body></html>"""
)


@pytest.mark.parametrize(
    "name, text",
    [
        ("outlook-plain-ru", OUTLOOK_PLAIN_RU),
        ("outlook-plain-en", OUTLOOK_PLAIN_EN),
        ("subject-first", BLOCK_STARTS_WITH_SUBJECT),
        ("owa-html", OWA_HTML),
        ("outlook-desktop-html", OUTLOOK_DESKTOP_HTML),
        ("inline-html", INLINE_HTML),
    ],
)
def test_header_block_never_reaches_body(name, text):
    """Тело письма — только новый вопрос, без шапки и без прежней переписки."""
    body = parse_email(as_message(text)).body
    assert body == QUESTION, f"{name}: разобрано как {body!r}"


def test_reply_written_under_the_quote_keeps_only_useful_text():
    """Ответ дописан под цитатой: раньше в промпт уезжал весь тред с шапкой.

    Отсечь цитату здесь нечем — она идёт первой, — но ни шапка, ни прошлый
    ответ модели попасть в реплику не должны: в таблице messages ответ уже
    лежит своей строкой, и второй его экземпляр — это тот самый повтор,
    от которого запрос рос квадратично.
    """
    bottom_posted = """От: Local LLM <llm@company.ru>
Отправлено: понедельник, 18 августа 2026 г. 10:15
Кому: Иванов Иван <ivanov@company.ru>
Тема: Re: Вопрос про Python

[Sofi] Есть три способа отсортировать список.

-- 
[Sofi] · сессия «Вопрос про Python»

""" + QUESTION

    body = parse_email(as_message(bottom_posted)).body

    assert QUESTION in body, "вопрос пользователя потерян"
    for marker in ("От:", "Отправлено:", "Кому:", "Тема:"):
        assert marker not in body, f"шапка цитаты уехала в промпт: {marker}"
    assert "Есть три способа" not in body, "прошлый ответ модели уехал в реплику вторым экземпляром"
    assert REPLY_MARKER not in body, "метка ответа модели осталась в реплике пользователя"


def test_own_reply_is_cut_by_its_own_marker():
    """Прошлый ответ модели вырезается по метке и подписи, текст вокруг остаётся."""
    text = (
        "Мой вопрос.\n\n"
        f"{REPLY_MARKER} Ответ модели, первый абзац.\n"
        "Второй абзац ответа.\n\n"
        "-- \n"
        f"{REPLY_MARKER} · сессия «Тема»\n\n"
        "И ещё вопрос."
    )

    assert strip_own_replies(text) == "Мой вопрос.\n\nИ ещё вопрос."


def test_own_reply_without_signature_loses_only_the_marker_line():
    """Ответ без подписи снимает одну строку: текст ниже принадлежит пользователю."""
    # клиент обрезает длинное письмо, и подпись до цитаты не доезжает.
    # отбрасывать хвост вслепую значило бы потерять вопрос пользователя
    text = f"{REPLY_MARKER} Начало ответа модели\nВопрос пользователя."

    assert strip_own_replies(text) == "Вопрос пользователя."


def test_users_own_text_with_a_single_colon_line_survives():
    """Одна строка вида «Кому: …» — текст пользователя, а не шапка цитаты."""
    text = "Подготовь письмо.\nКому: отделу продаж\nНужно к пятнице."
    assert strip_quoted(text) == text


def test_strip_header_blocks_removes_only_the_block():
    text = "Первая строка.\n\nОт: a@b.ru\nКому: c@d.ru\nТема: X\n\nОстальной текст."
    assert strip_header_blocks(text) == "Первая строка.\n\nОстальной текст."


# --- 2. Сборка контекста: история не отбрасывается ни при каком размере ------


def test_oversized_history_is_sent_whole(monkeypatch):
    """История, вышедшая за бюджет запроса, уходит в модель целиком.

    На уровне llm.build_messages отбор реплик под окно модели не выполняется:
    llm.warn_over_budget только логирует превышение и историю не трогает.
    Молчаливая потеря реплик здесь и была тем багом, из-за которого модель
    отвечала без учёта прежних писем — свёртку сессии от этого класса ошибок
    отделяет summarizer.fit_session, вызываемый раньше, в pipeline._answer.
    """
    monkeypatch.setattr(llm, "MAX_CONTEXT_CHARS", 24000)
    history = [
        {"role": "user", "body": "Первый вопрос"},
        {"role": "assistant", "body": "Первый ответ"},
        {"role": "user", "body": "Х" * 200_000},  # письмо, в тело которого уехал весь тред
    ]

    contents = [m.content for m in llm.build_messages(history, "Последний вопрос")]

    assert "Первый вопрос" in contents
    assert "Первый ответ" in contents
    assert "Х" * 200_000 in contents, "раздутая реплика выброшена из запроса"
    assert contents[-1] == "Последний вопрос"


def test_history_is_not_capped_by_message_count(allow_sender, fake_llm, sent_mail):
    """Число реплик сессии запрос не ограничивает: читаются все."""
    thread = "<n1@mail>"
    first = EmailMessage()
    first["From"] = FROM
    first["To"] = TO
    first["Subject"] = "Длинный тред"
    first["Message-ID"] = thread
    first["Date"] = "Tue, 18 Aug 2026 10:00:00 +0300"
    first.set_content("Вопрос 1", charset="utf-8")
    pipeline.process_email(email.message_from_bytes(first.as_bytes()))

    # двенадцать писем дают 24 реплики — больше прежнего потолка в тестах
    for number in range(2, 14):
        pipeline.process_email(
            reply_email(f"<n{number}@mail>", f"Вопрос {number}", sent_mail[-1]["message_id"])
        )

    assert len(storage.list_sessions()) == 1
    assert len(fake_llm[-1]["history"]) == 24, "часть реплик сессии не дошла до модели"


# --- 3. Пайплайн целиком: цепочка писем в одной сессии -----------------------


def outlook_reply(question: str, quoted: str) -> str:
    """Ответ Outlook desktop: новый текст, шапка, весь прежний тред."""
    return (
        f"{question}\n\n"
        "От: Local LLM <llm@company.ru>\n"
        "Отправлено: понедельник, 18 августа 2026 г. 10:15\n"
        "Кому: Иванов Иван <ivanov@company.ru>\n"
        "Тема: Re: Вопрос про Python\n\n"
        f"{quoted}"
    )


def subject_first_reply(question: str, quoted: str) -> str:
    """Шапка, у которой первое поле — «Тема:», а не «От:»."""
    return (
        f"{question}\n\n"
        "Тема: Re: Вопрос про Python\n"
        "От: Local LLM <llm@company.ru>\n"
        "Кому: Иванов Иван <ivanov@company.ru>\n\n"
        f"{quoted}"
    )


def inline_reply(question: str, quoted: str) -> str:
    """Шапка, приклеенная к тексту: клиент разделил их <span>, а не <br>."""
    return (
        f"{question}От: Local LLM <llm@company.ru>\n"
        "Отправлено: 18 августа 2026 г. 10:15 Кому: Иванов Иван Тема: Re: Вопрос\n\n"
        f"{quoted}"
    )


# Один и тот же человек пишет то из Outlook, то из OWA, то с телефона, поэтому
# формат цитаты меняется от письма к письму внутри одного треда.
REPLY_FORMATS = [outlook_reply, subject_first_reply, inline_reply]


def reply_email(message_id: str, body: str, in_reply_to: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = TO
    msg["Subject"] = "RE: Вопрос про Python"
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 18 Aug 2026 10:20:00 +0300"
    msg["In-Reply-To"] = in_reply_to
    msg.set_content(body, charset="utf-8")
    return email.message_from_bytes(msg.as_bytes())


def test_four_turn_thread_keeps_full_session_context(allow_sender, fake_llm, sent_mail):
    """Цепочка из четырёх писем: контекст сессии не теряется ни на одном шаге."""
    first = EmailMessage()
    first["From"] = FROM
    first["To"] = TO
    first["Subject"] = "Вопрос про Python"
    first["Message-ID"] = "<u1@mail>"
    first["Date"] = "Tue, 18 Aug 2026 10:00:00 +0300"
    first.set_content("Как отсортировать список?", charset="utf-8")
    pipeline.process_email(email.message_from_bytes(first.as_bytes()))

    questions = ["Давай пример кода.", "А если ключей несколько?", "Как это влияет на скорость?"]
    quoted = "Как отсортировать список?"
    for number, (question, formatter) in enumerate(zip(questions, REPLY_FORMATS), start=2):
        answer = sent_mail[-1]["body"]
        pipeline.process_email(
            reply_email(f"<u{number}@mail>", formatter(question, quoted), sent_mail[-1]["message_id"])
        )
        quoted = f"{answer}\n\n{quoted}"

    assert len(storage.list_sessions()) == 1, "тред рассыпался на несколько сессий"

    # каждое письмо доехало до модели как чистый вопрос
    assert [call["prompt"] for call in fake_llm] == ["Как отсортировать список?", *questions]

    # и с полной историей: на четвёртом письме перед моделью все шесть реплик
    last = fake_llm[-1]
    assert [row["body"] for row in last["history"]] == [
        "Как отсортировать список?",
        f"{REPLY_MARKER} ответ на: Как отсортировать список?",
        "Давай пример кода.",
        f"{REPLY_MARKER} ответ на: Давай пример кода.",
        "А если ключей несколько?",
        f"{REPLY_MARKER} ответ на: А если ключей несколько?",
    ]

    # ни в промптах, ни в истории нет ни шапок, ни осевшей в них цитаты
    for call in fake_llm:
        texts = [call["prompt"], *(row["body"] for row in call["history"])]
        for text in texts:
            for marker in LEAK_MARKERS:
                assert marker not in text, f"утечка цитаты в контекст: {marker}"


def test_thread_replies_do_not_grow_the_prompt(allow_sender, fake_llm, sent_mail):
    """Промпт не растёт от письма к письму: значит, тред в него не уезжает."""
    first = EmailMessage()
    first["From"] = FROM
    first["To"] = TO
    first["Subject"] = "Вопрос про Python"
    first["Message-ID"] = "<g1@mail>"
    first["Date"] = "Tue, 18 Aug 2026 10:00:00 +0300"
    first.set_content("Как отсортировать список?", charset="utf-8")
    pipeline.process_email(email.message_from_bytes(first.as_bytes()))

    quoted = "Как отсортировать список?"
    for number in range(2, 6):
        answer = sent_mail[-1]["body"]
        formatter = REPLY_FORMATS[number % len(REPLY_FORMATS)]
        pipeline.process_email(
            reply_email(f"<g{number}@mail>", formatter(QUESTION, quoted), sent_mail[-1]["message_id"])
        )
        quoted = f"{answer}\n\n{quoted}"

    assert all(call["prompt"] == QUESTION for call in fake_llm[1:])


def test_session_context_grows_linearly(allow_sender, fake_llm, sent_mail):
    """Контекст сессии — одна цепочка реплик: длина растёт линейно, а не квадратично.

    Цитата треда в реплику не пишется, поэтому суммарный объём запроса зависит
    от числа писем, а не от их числа в квадрате.
    """
    first = EmailMessage()
    first["From"] = FROM
    first["To"] = TO
    first["Subject"] = "Вопрос про Python"
    first["Message-ID"] = "<l1@mail>"
    first["Date"] = "Tue, 18 Aug 2026 10:00:00 +0300"
    first.set_content("Как отсортировать список?", charset="utf-8")
    pipeline.process_email(email.message_from_bytes(first.as_bytes()))

    quoted = "Как отсортировать список?"
    for number in range(2, 10):
        answer = sent_mail[-1]["body"]
        formatter = REPLY_FORMATS[number % len(REPLY_FORMATS)]
        pipeline.process_email(
            reply_email(f"<l{number}@mail>", formatter(QUESTION, quoted), sent_mail[-1]["message_id"])
        )
        quoted = f"{answer}\n\n{quoted}"

    # объём истории на каждом письме: у линейного роста разность соседних
    # значений постоянна, у квадратичного она растёт с каждым шагом
    sizes = [sum(len(row["body"]) for row in call["history"]) for call in fake_llm]
    steps = [b - a for a, b in zip(sizes, sizes[1:])]

    # первый шаг выпадает из ряда: у первого письма свой текст, дальше идёт
    # один и тот же вопрос, и постоянный шаг означает линейный рост
    assert len(set(steps[1:])) == 1, f"история растёт не линейно: шаги {steps}"

    # длина реплики равна длине текста письма: цитата в неё не попала
    assert all(len(row["body"]) <= len(QUESTION) + len(REPLY_MARKER) + 20
               for row in fake_llm[-1]["history"]), "в реплику осела цитата"
