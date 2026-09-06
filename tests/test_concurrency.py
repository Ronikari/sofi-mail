# тесты параллельной обработки писем пулом потоков.
# порядок: подменённая генерация считает одновременные вызовы -> run_once
# проходит по списку писем в несколько потоков -> утверждение проверяет пик
# параллельности, число сессий и порядок реплик.
# вход: FakeTransport и фикстуры allow_sender, thread_by_subject из conftest.py,
# функция make_email из test_pipeline.py.
# выход: результат pytest.
# проверяются замки _session_create_lock и _session_lock в pipeline.py.
# тесты опираются на паузы 0.15-0.2 с: они задают окно, в котором пересечение
# потоков наблюдаемо.
# запуск: pytest tests/test_concurrency.py

import threading
import time

import pytest

from src import pipeline, storage
from tests.conftest import FakeTransport
from tests.test_pipeline import make_email


# выход: список peak, куда на каждый вызов пишется число генераций, идущих
# в этот момент.
# побочный эффект: подмена llm.generate.
# пауза 0.2 с создаёт окно, в котором параллельные вызовы пересекаются
@pytest.fixture
def slow_llm(monkeypatch):
    """Подменяет генерацию задержкой и считает одновременные вызовы."""
    from src import llm

    # active работает счётчиком текущих вызовов, peak хранит его замеры
    active = []
    peak = []
    lock = threading.Lock()

    def generate(history, prompt, files=()):
        # замок нужен обеим правкам списков: вызовы идут из разных потоков
        with lock:
            active.append(1)
            peak.append(len(active))

        time.sleep(0.2)

        with lock:
            active.pop()
        return f"ответ на: {prompt[:40]}"

    monkeypatch.setattr(llm, "generate", generate)
    return peak


def test_emails_are_processed_in_parallel(allow_sender, slow_llm):
    """Письма разных сессий обрабатываются одновременно."""
    emails = [make_email(f"Тема {i}", f"<u{i}@mail>", f"Вопрос {i}") for i in range(3)]
    transport = FakeTransport(emails)

    started = time.monotonic()
    summary = pipeline.run_once(transport, workers=3)
    elapsed = time.monotonic() - started

    assert summary.answered == 3

    # пик выше 1 означает пересечение генераций во времени
    assert max(slow_llm) > 1, "генерации шли строго по очереди — параллельности нет"

    # порог 0.6 с: последовательный проход занял бы 3 паузы по 0.2 с
    assert elapsed < 0.6, f"три письма по 0.2 с заняли {elapsed:.2f} с — похоже на последовательный проход"


def test_same_subject_does_not_create_two_sessions(allow_sender, slow_llm, thread_by_subject):
    """Письма одной темы в одном проходе попадают в одну сессию."""
    # без замка _session_create_lock письма одновременно не находят сессию
    # и создают каждое свою
    emails = [make_email("Одна тема", f"<u{i}@mail>", f"Вопрос {i}") for i in range(4)]
    transport = FakeTransport(emails)

    pipeline.run_once(transport, workers=4)

    assert len(storage.list_sessions()) == 1


def test_replies_of_one_session_are_serialised(allow_sender, monkeypatch):
    """Реплики одной сессии генерируются по очереди."""
    # без замка _session_lock оба письма прочитали бы одну историю, и каждый
    # ответ собрался бы без учёта второго вопроса
    from src import llm

    # concurrent хранит замеры одновременности, active — счётчик текущих вызовов
    concurrent = []
    active = []
    lock = threading.Lock()

    def generate(history, prompt, files=()):
        with lock:
            active.append(1)
            concurrent.append(len(active))

        time.sleep(0.15)

        with lock:
            active.pop()
        return f"ответ на: {prompt[:40]}"

    monkeypatch.setattr(llm, "generate", generate)

    # первое письмо создаёт сессию, остальные — ответы в неё же
    transport = FakeTransport([make_email("Тема", "<u0@mail>", "Первый вопрос")])
    pipeline.run_once(transport, workers=1)
    root = transport.sent[0]["message_id"]

    # три ответа на одно письмо модели попадают в одну сессию по заголовку
    # In-Reply-To
    followups = [
        make_email("Re: Тема", f"<f{i}@mail>", f"Вопрос {i}", in_reply_to=root) for i in range(3)
    ]
    transport = FakeTransport(followups)

    # замеры первого прохода отбрасываются: измеряется только второй
    concurrent.clear()

    pipeline.run_once(transport, workers=3)

    assert len(storage.list_sessions()) == 1
    assert max(concurrent) == 1, "две реплики одной сессии генерировались одновременно"

    # четыре пары «вопрос — ответ»: первое письмо и три ответа на него
    roles = [row["role"] for row in storage.get_history(1, 40)]
    assert roles == ["user", "assistant"] * 4


def test_marks_seen_only_after_processing(allow_sender, slow_llm):
    """Каждое письмо помечается прочитанным один раз после отправки ответа."""
    emails = [make_email(f"Тема {i}", f"<u{i}@mail>") for i in range(3)]
    transport = FakeTransport(emails)

    pipeline.run_once(transport, workers=3)

    # дескрипторы заглушки — индексы писем; sorted убирает влияние порядка
    # завершения потоков
    assert sorted(transport.seen) == [0, 1, 2]
    assert len(transport.sent) == 3


def test_seen_flags_go_in_one_request(allow_sender, slow_llm):
    """Признак прочитанности ставится одним запросом на всю пачку."""
    # проход после простоя демона приносит десятки писем, и запрос на каждое
    # дал бы столько же последовательных обращений к Exchange перед следующим
    # опросом ящика
    emails = [make_email(f"Тема {i}", f"<u{i}@mail>") for i in range(5)]
    transport = FakeTransport(emails)

    pipeline.run_once(transport, workers=3)

    assert transport.bulk_calls == 1, "письма отмечались по одному"
    assert sorted(transport.seen) == [0, 1, 2, 3, 4]


def test_no_request_when_nothing_to_mark(allow_sender, slow_llm):
    """Пустой список писем к отметке запроса не порождает."""
    transport = FakeTransport([])

    pipeline.run_once(transport, workers=3)

    assert transport.bulk_calls == 0
