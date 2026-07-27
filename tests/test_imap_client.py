"""Устойчивость IMAP-клиента к разрыву соединения (без сети).

Сервер закрывает простаивающий сокет, пока модель генерирует ответ, поэтому
`socket error: EOF` на следующей команде — штатная ситуация, а не сбой.
"""

import imaplib

import pytest

from src import imap_client


class FakeConn:
    """Соединение, которое либо отвечает, либо изображает закрытый сокет."""

    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.commands = []

    def uid(self, name, *args):
        self.commands.append(name)
        if not self.alive:
            raise imaplib.IMAP4.abort("command: UID => socket error: EOF")
        return "OK", [b"7"]

    def close(self):
        pass

    def logout(self):
        pass


@pytest.fixture
def client(monkeypatch):
    """Клиент с мёртвым соединением; переподключение даёт живое."""
    instance = imap_client.IMAPClient()
    instance._conn = FakeConn(alive=False)

    reconnects = []

    def fake_connect():
        reconnects.append(True)
        instance._conn = FakeConn(alive=True)

    monkeypatch.setattr(instance, "connect", fake_connect)
    return instance, reconnects


def test_command_survives_dropped_connection(client):
    """Разрыв не должен ронять проход демона: команда повторяется на новом сокете."""
    instance, reconnects = client

    instance.mark_seen(b"7")

    assert len(reconnects) == 1
    assert instance._conn.commands == ["STORE"], "команда должна быть повторена после reconnect"


def test_second_failure_propagates(monkeypatch):
    """Если и после переподключения EOF — наверх, чтобы цикл ушёл на backoff."""
    instance = imap_client.IMAPClient()
    instance._conn = FakeConn(alive=False)
    monkeypatch.setattr(instance, "connect", lambda: setattr(instance, "_conn", FakeConn(alive=False)))

    with pytest.raises(imaplib.IMAP4.abort):
        instance.mark_seen(b"7")


def test_fetch_unseen_reconnects_on_search(client):
    """Первая же команда прохода тоже защищена — не только STORE."""
    instance, reconnects = client

    # FETCH вернёт "OK" без tuple-payload — письмо будет пропущено с warning,
    # нам здесь важно только то, что SEARCH пережил разрыв
    assert instance.fetch_unseen() == []
    assert len(reconnects) == 1
