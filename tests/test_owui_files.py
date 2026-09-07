# тесты выключателя удаления файлов в Open WebUI.
# порядок: ATTACHMENT_DELETE_ENABLED подменяется через monkeypatch -> функция
# модуля вызывается -> утверждение проверяет, что сетевой запрос не понадобился.
# вход: monkeypatch.
# выход: результат pytest.
# проверяется owui_files.py; обе ветки обрываются до вызова _client,
# поэтому сеть здесь не нужна.
# запуск: pytest tests/test_owui_files.py

from src import owui_files


def test_delete_is_refused_when_disabled(monkeypatch):
    """Выключатель оставляет файл в хранилище и сообщает об отказе."""
    # значение False возвращается намеренно: forget не поставит отметку
    # deleted_at, и строка таблицы продолжит соответствовать хранилищу
    monkeypatch.setattr(owui_files, "ATTACHMENT_DELETE_ENABLED", False)

    assert owui_files.delete("file-1") is False


def test_purge_expired_stops_before_the_network_when_disabled(monkeypatch):
    """Выключатель обрывает уборку по сроку до обращения к серверу."""
    monkeypatch.setattr(owui_files, "ATTACHMENT_DELETE_ENABLED", False)

    # первый элемент — удалённые файлы, второй — оставшиеся; уборка
    # не выполнялась, поэтому оба нулевые
    assert owui_files.purge_expired(30) == (0, 0)


def test_forget_keeps_the_row_alive_when_deletion_is_disabled(monkeypatch):
    """Выключатель оставляет строку файла живой в таблице session_files."""
    # отметка deleted_at ставится по факту удаления на стороне Open WebUI:
    # при выключенном удалении файл остаётся, и строка должна это отражать
    monkeypatch.setattr(owui_files, "ATTACHMENT_DELETE_ENABLED", False)

    marked = []
    from src import storage

    monkeypatch.setattr(storage, "mark_file_deleted", lambda file_id: marked.append(file_id))

    assert owui_files.forget(["file-1", "file-2"]) == 0
    assert marked == []


def test_purge_expired_returns_zero_for_unlimited_retention():
    """Срок хранения 0 отключает уборку файлов."""
    assert owui_files.purge_expired(0) == (0, 0)


def test_reference_carries_no_context_field():
    """Режим подачи документа выбирает Open WebUI, а не отправитель запроса.

    Поле context=full раньше ставилось здесь по результатам локального разбора.
    Разбора нет, и решение перешло на сторону сервера: фокусированный поиск он
    включает сам, подачу целиком включает инструмент full_context_tool.py.
    """
    assert owui_files.reference("file-1") == {"type": "file", "id": "file-1"}
