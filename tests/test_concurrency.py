"""Параллельная обработка писем: что должно ускоряться, а что — остаться строгим."""

import itertools
import threading
import time

import pytest

# Message-ID должны быть уникальны в пределах теста: messages.message_id — UNIQUE,
# и совпадение молча отбросит реплику (INSERT OR IGNORE)
_sent_counter = itertools.count()

from src import pipeline, storage
from tests.test_pipeline import make_email


class FakeTransport:
    """Транспорт-заглушка: отдаёт заранее подготовленные письма."""

    def __init__(self, emails):
        self.emails = list(enumerate(emails))
        self.sent = []
        self.seen = []
        self._lock = threading.Lock()

    def fetch_unseen(self):
        return self.emails

    def mark_seen(self, handle):
        self.seen.append(handle)

    def unsee_by_message_id(self, message_id):
        return 1

    def send_reply(self, to_address, subject, body, session_title, in_reply_to=None, references=None):
        with self._lock:
            message_id = f"<sent-{next(_sent_counter)}@llm>"
            self.sent.append({"to": to_address, "body": body, "message_id": message_id})
        return message_id

    def reconnect(self):
        pass

    def close(self):
        pass

    def describe(self):
        return "fake"


@pytest.fixture
def slow_llm(monkeypatch):
    """Генерация с задержкой — иначе параллельность нечем измерить."""
    from src import llm

    active = []
    peak = []
    lock = threading.Lock()

    def generate(history, prompt):
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
    """Три письма от разных тем должны обрабатываться одновременно, а не по очереди."""
    emails = [make_email(f"Тема {i}", f"<u{i}@mail>", f"Вопрос {i}") for i in range(3)]
    transport = FakeTransport(emails)

    started = time.monotonic()
    summary = pipeline.run_once(transport, workers=3)
    elapsed = time.monotonic() - started

    assert summary.answered == 3
    assert max(slow_llm) > 1, "генерации шли строго по очереди — параллельности нет"
    assert elapsed < 0.6, f"три письма по 0.2 с заняли {elapsed:.2f} с — похоже на последовательный проход"


def test_same_subject_does_not_create_two_sessions(allow_sender, slow_llm):
    """Два письма одной темы в одном проходе — одна сессия, а не две.

    Без блокировки на «найти-или-создать» оба письма не находят сессию
    одновременно и создают каждое свою.
    """
    emails = [make_email("Одна тема", f"<u{i}@mail>", f"Вопрос {i}") for i in range(4)]
    transport = FakeTransport(emails)

    pipeline.run_once(transport, workers=4)

    assert len(storage.list_sessions()) == 1


def test_replies_of_one_session_are_serialised(allow_sender, monkeypatch):
    """Реплики одной сессии не должны генерироваться одновременно.

    Иначе оба письма прочитают одну историю, и каждый ответ будет построен
    без учёта второго вопроса.
    """
    from src import llm

    concurrent = []
    active = []
    lock = threading.Lock()

    def generate(history, prompt):
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

    followups = [
        make_email("Re: Тема", f"<f{i}@mail>", f"Вопрос {i}", in_reply_to=root) for i in range(3)
    ]
    transport = FakeTransport(followups)
    concurrent.clear()

    pipeline.run_once(transport, workers=3)

    assert len(storage.list_sessions()) == 1
    assert max(concurrent) == 1, "две реплики одной сессии генерировались одновременно"
    roles = [row["role"] for row in storage.get_history(1, 40)]
    assert roles == ["user", "assistant"] * 4


def test_marks_seen_only_after_processing(allow_sender, slow_llm):
    """Письмо помечается обработанным после ответа, и ровно один раз."""
    emails = [make_email(f"Тема {i}", f"<u{i}@mail>") for i in range(3)]
    transport = FakeTransport(emails)

    pipeline.run_once(transport, workers=3)

    assert sorted(transport.seen) == [0, 1, 2]
    assert len(transport.sent) == 3
