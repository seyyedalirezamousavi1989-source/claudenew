import json
import pytest
from local_chat.conversation_store import ConversationStore


def test_conversation_store_round_trip_and_restart(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    store = ConversationStore(path)
    data = [{"id": "conversation_123", "title": "پروژه", "messages": [
        {"role": "user", "content": "ادامه بده", "requestId": "request_123456"},
        {"role": "assistant", "content": "در حال انجام"},
        {"role": "system", "content": "نباید ذخیره شود"},
    ]}]
    assert store.replace_all(data) == {"saved": 1}
    restarted = ConversationStore(path)
    restored = restarted.list_all()
    assert len(restored) == 1
    assert [m["role"] for m in restored[0]["messages"]] == ["user", "assistant"]
    assert restored[0]["messages"][0]["requestId"] == "request_123456"


def test_empty_snapshot_never_erases_durable_archive(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    store.replace_all([{"id": "conversation_123", "title": "x", "messages": []}])
    store.replace_all([])
    assert len(store.list_all()) == 1


def test_rejects_oversized_or_invalid_snapshot(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    with pytest.raises(ValueError):
        store.replace_all(None)
    with pytest.raises(ValueError):
        store.replace_all([{"id": "bad id", "messages": []}] * 1 + [
            {"id": f"conversation_{i:03d}", "messages": []} for i in range(101)
        ])


def test_attachment_bytes_and_untrusted_roles_are_not_persisted(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    store.replace_all([{"id": "conversation_123", "messages": [
        {"role": "tool", "content": "safe", "attachments": [{"id": "a1", "name": "x.pdf"}]},
        {"role": "system", "content": "secret prompt"},
    ]}])
    message = store.list_all()[0]["messages"][0]
    assert message["content"] == "safe"
    assert message["attachments"][0]["id"] == "a1"
    assert all(m["role"] != "system" for m in store.list_all()[0]["messages"])


def test_frontend_has_debounced_local_archive_sync():
    from pathlib import Path
    js = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "restoreConversationsFromServer" in js
    assert "syncConversationsToServer" in js
    assert "conversationSyncTimer = setTimeout" in js
