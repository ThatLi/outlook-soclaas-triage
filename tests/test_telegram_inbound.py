from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from outlook_triage.database import Database
from outlook_triage.models import EmailClassification
from outlook_triage.telegram import TelegramError
from outlook_triage.telegram_inbound import ensure_polling_available, listen, poll_once, process_updates


def _seed_task(db: Database, source_id: str = "entry-1", subject: str = "Private <report>") -> None:
    db.store_sync_batch(
        [{
            "messageKey": f"store-1:{source_id}", "sourceId": source_id, "storeId": "store-1",
            "sender": {"emailAddress": {"name": "Alice", "address": "a@example.test"}},
            "subject": subject, "receivedDateTime": "2026-10-10T08:00:00+08:00",
        }],
        "2026-10-10T08:00:00+08:00",
    )
    db.save_classification(
        f"store-1:{source_id}",
        EmailClassification.model_validate({
            "requires_action": True, "urgency": "normal", "action_type": "other",
            "task": "Review it", "deadline": None, "deadline_raw": None,
            "summary": "Summary", "category": "action", "reason": "Requested", "confidence": 1,
        }),
        "model",
    )


class Telegram:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send_message(self, text, **kwargs):
        if self.fail:
            raise TelegramError("offline", category="transient")
        self.messages.append(text)

    def send_plain_message(self, text, **kwargs):
        self.send_message(text)

    def get_updates(self, *, offset=None, timeout=30):
        return []

    def webhook_info(self):
        return {"url": ""}


class Outlook:
    read_calls = 0
    read_items = []
    show_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def mark_read(self, source_id, store_id):
        type(self).read_calls += 1
        type(self).read_items.append(source_id)
        return True

    def get_body_preview(self, source_id, store_id, *, limit):
        type(self).show_calls += 1
        return "body <private>"


def _update(update_id, text, chat=7):
    return {"update_id": update_id, "message": {
        "chat": {"id": chat, "type": "private"},
        "from": {"id": chat, "is_bot": False}, "text": text,
    }}


@pytest.fixture(autouse=True)
def reset_outlook():
    Outlook.read_calls = 0
    Outlook.read_items = []
    Outlook.show_calls = 0


def test_authorized_read_is_applied_and_audited_without_sensitive_data(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    telegram = Telegram()
    try:
        _seed_task(db)
        assert process_updates(db, configured, telegram, [_update(10, "/read #1")], logging.getLogger(), outlook_factory=Outlook) == 1
        assert Outlook.read_calls == 1
        assert db.telegram_offset() == 11
        row = db.telegram_update(10)
        assert row["status"] == "responded"
        assert dict(row)["command"] == "read"
        assert "entry-1" not in str(dict(row))
        assert "Private &lt;report&gt;" in telegram.messages[0]
    finally:
        db.close()


def test_duplicate_read_does_not_repeat_mutation(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    try:
        _seed_task(db)
        update = _update(10, "/read #1")
        process_updates(db, configured, Telegram(), [update], logging.getLogger(), outlook_factory=Outlook)
        process_updates(db, configured, Telegram(), [update], logging.getLogger(), outlook_factory=Outlook)
        assert Outlook.read_calls == 1
    finally:
        db.close()


def test_read_accepts_multiple_deduplicated_task_ids(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    telegram = Telegram()
    try:
        _seed_task(db, "entry-1", "First")
        _seed_task(db, "entry-2", "Second")
        process_updates(
            db, configured, telegram, [_update(11, "/read #1 #2 #1")],
            logging.getLogger(), outlook_factory=Outlook,
        )
        assert Outlook.read_items == ["entry-1", "entry-2"]
        assert db.telegram_update_task_ids(11, status="applied") == [1, 2]
        assert "2 emails are marked as read" in telegram.messages[0]
        assert telegram.messages[0].count("#1") == 1
    finally:
        db.close()


def test_multi_read_validates_all_task_ids_before_changing_outlook(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    telegram = Telegram()
    try:
        _seed_task(db)
        process_updates(
            db, configured, telegram, [_update(12, "/read #1 #999")],
            logging.getLogger(), outlook_factory=Outlook,
        )
        assert Outlook.read_calls == 0
        assert "#999" in telegram.messages[0]
        assert "No emails were changed" in telegram.messages[0]
    finally:
        db.close()


def test_show_returns_escaped_body_without_reading_state_change(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    telegram = Telegram()
    try:
        _seed_task(db)
        process_updates(db, configured, telegram, [_update(2, "/show 1")], logging.getLogger(), outlook_factory=Outlook)
        assert Outlook.show_calls == 1
        assert Outlook.read_calls == 0
        assert "body &lt;private&gt;" in telegram.messages[0]
    finally:
        db.close()


def test_unauthorized_chat_is_silently_ignored(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    telegram = Telegram()
    try:
        _seed_task(db)
        process_updates(db, configured, telegram, [_update(3, "/show 1", chat=8)], logging.getLogger(), outlook_factory=Outlook)
        assert telegram.messages == []
        assert db.telegram_update(3) is None
        assert db.telegram_offset() == 4
    finally:
        db.close()


def test_transient_response_failure_leaves_offset_unadvanced(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    try:
        _seed_task(db)
        with pytest.raises(TelegramError):
            process_updates(db, configured, Telegram(fail=True), [_update(4, "/read 1")], logging.getLogger(), outlook_factory=Outlook)
        assert db.telegram_offset() is None
        assert db.telegram_update(4)["status"] == "applied"
        process_updates(db, configured, Telegram(), [_update(4, "/read 1")], logging.getLogger(), outlook_factory=Outlook)
        assert Outlook.read_calls == 1
    finally:
        db.close()


def test_poll_once_uses_saved_offset(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)

    class PollingTelegram(Telegram):
        def __init__(self):
            super().__init__()
            self.polls = []

        def get_updates(self, *, offset=None, timeout=30):
            self.polls.append((offset, timeout))
            return [_update(12, "/help")]

    telegram = PollingTelegram()
    try:
        db.set_telegram_offset(12)
        assert poll_once(db, configured, telegram, logging.getLogger(), timeout=0) == 1
        assert telegram.polls == [(12, 0)]
        assert db.telegram_offset() == 13
        db.set_telegram_offset(10)
        assert db.telegram_offset() == 13
    finally:
        db.close()


def test_listener_backs_off_after_transient_failure(settings):
    configured = replace(settings, telegram_chat_id="7")
    db = Database(settings.database_file)
    delays = []

    class FlakyTelegram(Telegram):
        calls = 0

        def get_updates(self, *, offset=None, timeout=30):
            self.calls += 1
            if self.calls == 1:
                raise TelegramError("offline", category="transient")
            return []

    try:
        assert listen(db, configured, FlakyTelegram(), logging.getLogger(), max_cycles=2, sleep=delays.append) == 0
        assert delays == [1.0]
    finally:
        db.close()


def test_webhook_blocks_polling():
    telegram = Telegram()
    telegram.webhook_info = lambda: {"url": "https://example.test/hook"}
    with pytest.raises(TelegramError, match="webhook"):
        ensure_polling_available(telegram)
