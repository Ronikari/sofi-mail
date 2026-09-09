# тесты свёртки переписки сессии в сводку.
# порядок: разбор просьбы и подсчёт объёма проверяются функциями напрямую ->
# автоматическая свёртка и свёртка по письму проходят полный путь письма
# через pipeline.process_email -> утверждение читает базу, вызовы модели
# и отправленные письма.
# вход: фикстуры allow_sender, sent_mail, transport из conftest.py и локальная
# заглушка модели, отличающая запрос на суммаризацию от обычного вопроса.
# выход: результат pytest.
# проверяются summarizer.py, storage.py (таблица summaries) и ветки pipeline.py.
# запуск: pytest tests/test_summarizer.py
#
# значения настроек патчатся в модуле summarizer, а не в config: модуль забрал
# их к себе выражением `from src.config import ...`, и подмена в config до него
# не доходит

import pytest

from src import pipeline, storage, summarizer
from src.email_parser import REPLY_MARKER
from tests.test_pipeline import make_email, make_email_with_doc

# текст, который заглушка отдаёт на запрос суммаризации: по нему тесты
# отличают сводку от обычного ответа модели
SUMMARY_TEXT = "Пользователь спрашивал про отчёт, Sofi отвечал по срокам."


# выход: список вызовов модели; каждый элемент хранит историю и текст запроса.
# побочный эффект: подмена llm.generate.
# заглушка отличает суммаризацию от вопроса по первой строке запроса: шаблон
# summarizer._INSTRUCTION начинается со слова «Сверни»
@pytest.fixture
def fake_llm(monkeypatch):
    """Подменяет модель заглушкой, различающей вопрос и запрос на сводку."""
    from src import llm

    calls = []

    def generate(history, prompt, files=()):
        calls.append({"history": [dict(row) for row in history], "prompt": prompt})

        if prompt.startswith("Сверни переписку"):
            return SUMMARY_TEXT
        return f"ответ на: {prompt[:40]}"

    monkeypatch.setattr(llm, "generate", generate)
    return calls


# выход: список вызовов модели без ответа на запрос суммаризации.
# по нему проверяется контекст, с которым модель отвечала на письма
def answers(calls):
    """Оставляет вызовы модели, в которых её просили ответить на письмо."""
    return [call for call in calls if not call["prompt"].startswith("Сверни переписку")]


# --- разбор просьбы о свёртке -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "/summary",
        "/SUMMARY по нашей переписке",  # регистр команды не важен
    ],
)
def test_request_is_recognised(text):
    """Свёртку запускает только команда /summary первой строкой."""
    assert summarizer.is_summary_request(text)


@pytest.mark.parametrize(
    "text",
    [
        # синонимы команды сняты: документирован один литерал
        "/сводка",
        "/суммаризация",
        "/суммаризируй",
        # просьба словами свёртку не запускает: она необратима и стирает
        # контекст сессии, а те же обороты встречаются в письме по делу
        "Суммаризируй, пожалуйста, нашу переписку",
        "Сверни, пожалуйста, диалог в сводку",
        "Подведи итог обсуждения в этой сессии",
        "Сделай краткую сводку по нашей переписке",
        "Нужна сводка по этому треду",
        "Подведи итог по вложенному документу",
        "Что там с нашей перепиской по срокам?",
        "Спасибо, отличная сводка! Что там по нашему разговору с юристом?",
        "Ок, сводку понял. А что решили по бюджету обсуждения?",
        "Хорошая сводка получилась, но у меня ещё вопрос по контексту задачи",
        "А по регионам?",
        "",
    ],
)
def test_ordinary_letter_is_not_a_request(text):
    """Обычное письмо за просьбу о свёртке не принимается."""
    assert not summarizer.is_summary_request(text)


def test_command_works_in_a_long_letter():
    """Явная команда в первой строке действует независимо от длины письма."""
    assert summarizer.is_summary_request("/summary\n\n" + "Пояснение. " * 100)


# --- объём и коэффициент сжатия ---------------------------------------------


def test_target_length_follows_the_ratio(monkeypatch):
    """Целевая длина сводки считается по коэффициенту сжатия."""
    monkeypatch.setattr(summarizer, "SUMMARY_COMPRESSION_RATIO", 0.1)
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 100000)

    assert summarizer.target_chars(100000) == 10000

    # другой коэффициент даёт другую цель на той же переписке
    monkeypatch.setattr(summarizer, "SUMMARY_COMPRESSION_RATIO", 0.5)
    assert summarizer.target_chars(100000) == 50000


def test_target_length_has_a_floor_and_a_ceiling(monkeypatch):
    """Доля от короткой переписки не опускает цель ниже осмысленной длины."""
    monkeypatch.setattr(summarizer, "SUMMARY_COMPRESSION_RATIO", 0.2)
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 10000)

    # 0.2 от 200 символов — сорок знаков, в которые не помещается ни один вопрос
    assert summarizer.target_chars(200) == summarizer.MIN_TARGET_CHARS

    # потолок — половина предела сессии: после свёртки остаётся запас на письма
    assert summarizer.target_chars(1_000_000) == 5000


def test_limit_counts_the_current_letter(monkeypatch):
    """Предел сессии считается вместе с текстом текущего письма."""
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 100)
    history = [{"id": 1, "role": "user", "body": "я" * 60}, {"id": 2, "role": "assistant", "body": "и" * 30}]

    assert not summarizer.over_limit(history, "коротко")
    assert summarizer.over_limit(history, "я" * 30)


def test_single_summary_is_not_folded_again(monkeypatch):
    """Сессия из одной сводки повторной свёртке не подлежит."""
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 10)
    history = [{"id": 7, "role": storage.SUMMARY_ROLE, "body": "я" * 500}]

    # свёртка сводки самой в себя короче её не сделает, а запрос к модели
    # выполнялся бы на каждом письме
    assert not summarizer.over_limit(history, "вопрос")


# --- стенограмма ------------------------------------------------------------


def test_transcript_names_sender_and_recipient():
    """В стенограмме у каждой реплики указано, кто её написал и кому."""
    text = summarizer.transcript(
        [
            {"role": "user", "body": "Первый вопрос"},
            {"role": "assistant", "body": "Первый ответ"},
            {"role": storage.SUMMARY_ROLE, "body": "Старая сводка"},
        ],
        "ivan@company.ru",
    )

    assert "Пользователь (ivan@company.ru) → Sofi:\nПервый вопрос" in text
    assert "Sofi → Пользователь (ivan@company.ru):\nПервый ответ" in text
    assert "Сводка более ранней переписки:\nСтарая сводка" in text


def test_transcript_keeps_the_user_text_intact():
    """Строка [Sofi] в письме пользователя остаётся: её написал он."""
    text = summarizer.transcript(
        [
            {"role": "user", "body": f"{REPLY_MARKER} это я скопировал из вашего письма"},
            {"role": "assistant", "body": f"{REPLY_MARKER} а это мой ответ"},
        ],
        "ivan@company.ru",
    )

    # у реплики пользователя метка на месте, у реплики модели снята:
    # её автора уже называет подпись строки
    assert f"→ Sofi:\n{REPLY_MARKER} это я скопировал" in text
    assert "Sofi → Пользователь (ivan@company.ru):\nа это мой ответ" in text


def test_transcript_has_no_timestamps():
    """Время писем в стенограмму не попадает: в контексте оно ничего не решает."""
    text = summarizer.transcript(
        [{"role": "user", "body": "Вопрос", "created_at": "2026-09-07T12:00:00+00:00"}],
        "ivan@company.ru",
    )

    assert "2026" not in text


# --- автоматическая свёртка по пределу сессии -------------------------------


def test_session_is_folded_when_the_limit_is_reached(allow_sender, fake_llm, sent_mail, monkeypatch):
    """Достигнутый предел сессии сворачивает переписку до текущего письма."""
    # предел ниже объёма одной пары реплик: свёртка наступает на втором письме
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 30)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    summary = storage.get_summary(1)

    assert summary is not None
    assert summary["body"] == SUMMARY_TEXT
    assert summary["reason"] == summarizer.REASON_LIMIT

    # граница проходит по последней реплике до текущего письма: в сводку ушли
    # первый вопрос и первый ответ, само второе письмо в неё не входит
    assert summary["covers_upto"] == 2

    # свёртка по пределу проходит молча, и о потере подробностей переписки
    # пользователь узнаёт из предупреждения в первой строке ответа
    letter = sent_mail[-1]["body"]
    assert letter.startswith(f"{REPLY_MARKER} {pipeline.SUMMARY_DEGRADATION_NOTICE}")
    assert f"{pipeline.SUMMARY_DEGRADATION_NOTICE}\n\n" in letter

    # ответ на второе письмо собирался уже по сводке
    context = answers(fake_llm)[-1]["history"]
    assert [row["role"] for row in context] == [storage.SUMMARY_ROLE]
    assert context[0]["body"] == SUMMARY_TEXT


def test_folded_replicas_never_return_to_the_model(allow_sender, fake_llm, sent_mail, monkeypatch):
    """После свёртки прежние реплики в контекст не возвращаются."""
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 30)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "Секретный первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    # третье письмо цитирует первое, как это делает почтовый клиент
    quoted = (
        "Третий вопрос\n\n"
        "От: Sofi\nОтправлено: 7 сентября 2026 г.\nКому: Андрей\nТема: Тема\n\n"
        "> Секретный первый вопрос\n"
    )
    pipeline.process_email(
        make_email("Re: Тема", "<u3@mail>", quoted, in_reply_to=sent_mail[1]["message_id"])
    )

    last = answers(fake_llm)[-1]

    # ни история, ни текст письма не несут свёрнутой реплики
    assert "Секретный первый вопрос" not in "".join(row["body"] for row in last["history"])
    assert "Секретный первый вопрос" not in last["prompt"]

    # реплики остались в базе: свёртка меняет контекст, а не переписку
    assert any("Секретный первый вопрос" in row["body"] for row in storage.list_messages(1))


def test_folding_covers_replicas_outside_the_history_window(
    allow_sender, fake_llm, sent_mail, monkeypatch
):
    """Свёртка покрывает всю переписку, а не окно MAX_HISTORY_MESSAGES."""
    # pipeline читает историю с ограничением по числу реплик; свёртка обязана
    # взять переписку целиком, иначе реплики старше окна уходят за границу
    # covers_upto, не попав ни в одну сводку, и пропадают из контекста навсегда
    monkeypatch.setattr(pipeline, "MAX_HISTORY_MESSAGES", 2)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "Самый первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    # предел включается перед третьим письмом: свёртке подлежат четыре реплики,
    # из которых в окно pipeline попали только две последние
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 30)
    pipeline.process_email(
        make_email("Re: Тема", "<u3@mail>", "Третий вопрос", in_reply_to=sent_mail[1]["message_id"])
    )

    folds = [call for call in fake_llm if call["prompt"].startswith("Сверни переписку")]

    assert len(folds) == 1

    # переписка ушла в суммаризацию целиком, включая реплику за окном
    assert "Самый первый вопрос" in folds[0]["prompt"]
    assert "Второй вопрос" in folds[0]["prompt"]

    # граница совпала с последней репликой до текущего письма
    assert storage.get_summary(1)["covers_upto"] == 4


def test_request_covers_replicas_outside_the_history_window(
    allow_sender, fake_llm, sent_mail, monkeypatch
):
    """Свёртка по просьбе тоже берёт переписку целиком, а не последние реплики."""
    monkeypatch.setattr(pipeline, "MAX_HISTORY_MESSAGES", 2)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "Самый первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )
    pipeline.process_email(
        make_email("Re: Тема", "<u3@mail>", "/summary", in_reply_to=sent_mail[1]["message_id"])
    )

    folds = [call for call in fake_llm if call["prompt"].startswith("Сверни переписку")]

    assert "Самый первый вопрос" in folds[0]["prompt"]
    assert storage.get_summary(1)["covers_upto"] == storage.last_message_id(1)

    # ни одна реплика не осталась между сводкой и контекстом
    assert [row["role"] for row in storage.get_history(1)] == [storage.SUMMARY_ROLE]


def test_folding_failure_keeps_the_answer(allow_sender, fake_llm, sent_mail, monkeypatch):
    """Сбой суммаризации не отменяет ответ: запрос уходит полным контекстом."""
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 30)

    def broken(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    monkeypatch.setattr(summarizer, "summarize", broken)

    outcome = pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    assert outcome.status == "ok"
    assert storage.get_summary(1) is None

    # контекст остался полным: обе прежние реплики на месте
    assert len(answers(fake_llm)[-1]["history"]) == 2


def test_disabled_summarisation_leaves_the_session_alone(allow_sender, fake_llm, sent_mail, monkeypatch):
    """Выключенная свёртка оставляет сессию расти как прежде."""
    monkeypatch.setattr(summarizer, "SESSION_MAX_CHARS", 30)
    monkeypatch.setattr(summarizer, "SUMMARY_ENABLED", False)

    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )

    assert storage.get_summary(1) is None
    assert len(answers(fake_llm)[-1]["history"]) == 2


# --- свёртка по просьбе пользователя ----------------------------------------


def test_request_leaves_only_the_summary_in_context(allow_sender, fake_llm, sent_mail):
    """После просьбы о свёртке в контексте сессии остаётся одна сводка."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "Второй вопрос", in_reply_to=sent_mail[0]["message_id"])
    )
    pipeline.process_email(
        make_email(
            "Re: Тема", "<u3@mail>", "/summary",
            in_reply_to=sent_mail[1]["message_id"],
        )
    )

    context = storage.get_history(1)

    assert [row["role"] for row in context] == [storage.SUMMARY_ROLE]
    assert context[0]["body"] == SUMMARY_TEXT

    # граница покрывает и письмо с просьбой, и ответ на него: обе реплики
    # записаны якорями треда, но в контекст не идут
    assert storage.get_summary(1)["covers_upto"] == storage.last_message_id(1)
    assert storage.get_summary(1)["reason"] == summarizer.REASON_REQUEST


def test_request_is_answered_with_the_summary(allow_sender, fake_llm, sent_mail):
    """Ответом на просьбу служит сама сводка, а не обычная генерация."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "/summary", in_reply_to=sent_mail[0]["message_id"])
    )

    letter = sent_mail[-1]["body"]

    assert letter.startswith(REPLY_MARKER)
    assert SUMMARY_TEXT in letter
    assert pipeline.SUMMARY_DEGRADATION_NOTICE in letter
    assert f"{pipeline.SUMMARY_DEGRADATION_NOTICE}\n\n" in letter

    # обычной генерации на это письмо не было: последний вопрос модели —
    # первое письмо сессии
    assert answers(fake_llm)[-1]["prompt"] == "Первый вопрос"


def test_reply_to_the_summary_letter_stays_in_the_session(allow_sender, fake_llm, sent_mail):
    """Ответ на письмо со сводкой продолжает ту же сессию."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))
    pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "/summary", in_reply_to=sent_mail[0]["message_id"])
    )
    pipeline.process_email(
        make_email("Re: Тема", "<u3@mail>", "Третий вопрос", in_reply_to=sent_mail[-1]["message_id"])
    )

    # письмо со сводкой осталось якорем треда: новой сессии не завелось
    assert len(storage.list_sessions()) == 1

    # контекст ответа — сводка, свежая реплика в неё не входит
    assert [row["role"] for row in answers(fake_llm)[-1]["history"]] == [storage.SUMMARY_ROLE]


def test_request_in_an_empty_session_is_refused(allow_sender, fake_llm, sent_mail):
    """Просьба свернуть пустую сессию сводки не создаёт."""
    outcome = pipeline.process_email(make_email("Тема", "<u1@mail>", "/summary"))

    assert outcome.status == "skipped"
    assert storage.get_summary(1) is None
    assert pipeline.SUMMARY_EMPTY_NOTICE in sent_mail[-1]["body"]


def test_request_with_a_document_is_an_ordinary_question(
    allow_sender, fake_llm, sent_mail, fake_owui_files
):
    """Просьба при вложении относится к документу и контекст не стирает."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))

    # то же письмо без вложения свёртку бы запустило
    assert summarizer.is_summary_request("/summary")

    pipeline.process_email(
        make_email_with_doc(
            "Re: Тема", "<u2@mail>", body="/summary",
            in_reply_to=sent_mail[0]["message_id"],
        )
    )

    assert storage.get_summary(1) is None

    # письмо прошло обычной генерацией: вопрос дошёл до модели вместе
    # с историей сессии
    assert answers(fake_llm)[-1]["prompt"].endswith("/summary")


def test_failed_summary_on_request_keeps_the_context(allow_sender, fake_llm, sent_mail, monkeypatch):
    """Неудавшаяся суммаризация оставляет переписку нетронутой."""
    pipeline.process_email(make_email("Тема", "<u1@mail>", "Первый вопрос"))

    def broken(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(summarizer, "summarize", broken)

    outcome = pipeline.process_email(
        make_email("Re: Тема", "<u2@mail>", "/summary", in_reply_to=sent_mail[0]["message_id"])
    )

    assert outcome.status == "error"
    assert storage.get_summary(1) is None

    # пользователю ушло письмо с причиной, а не пустой ответ
    assert "Не удалось получить ответ модели" in sent_mail[-1]["body"]

    # контекст прежний: реплики первой пары на месте
    assert [row["role"] for row in storage.get_history(1)] == ["user", "assistant"]
