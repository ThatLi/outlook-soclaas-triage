from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from outlook_triage.database import Database
from outlook_triage.telegram import TelegramError
from outlook_triage import telegram_delivery


NOW = datetime.fromisoformat("2026-10-10T08:00:00+08:00")


def _enqueue(db: Database, digest_date: str, chunks: list[str], created_at: datetime):
    return db.enqueue_telegram_delivery(digest_date, chunks, created_at.isoformat(timespec="seconds"))


def _configured(settings):
    return replace(settings, telegram_bot_token="token", telegram_chat_id="7")


def test_outbox_reuses_active_delivery_and_freezes_chunks(settings):
    db = Database(settings.database_file)
    try:
        first = _enqueue(db, "2026-10-10", ["original"], NOW)
        second = _enqueue(db, "2026-10-10", ["replacement"], NOW + timedelta(minutes=5))

        assert first["id"] == second["id"]
        assert db.telegram_delivery_chunks(second) == ["original"]

        db.mark_telegram_chunk_sent(int(first["id"]), 1, 1, NOW.isoformat())
        third = _enqueue(db, "2026-10-10", ["manual resend"], NOW + timedelta(minutes=10))
        assert third["id"] != first["id"]
    finally:
        db.close()


def test_deliveries_are_sent_oldest_first(monkeypatch, settings):
    db = Database(settings.database_file)
    sent = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, text):
            sent.append(text)

    monkeypatch.setattr(telegram_delivery, "TelegramClient", Client)
    try:
        _enqueue(db, "2026-10-09", ["older-1", "older-2"], NOW - timedelta(days=1))
        _enqueue(db, "2026-10-10", ["newer"], NOW)

        result = telegram_delivery.deliver_pending(
            db, _configured(settings), logging.getLogger("test"), now=NOW
        )

        assert sent == ["older-1", "older-2", "newer"]
        assert result.completed_deliveries == 2
        assert result.delivered_messages == 3
        assert len(db.telegram_deliveries_with_status("delivered")) == 2
    finally:
        db.close()


def test_partial_delivery_resumes_with_one_delayed_notice(monkeypatch, settings):
    db = Database(settings.database_file)
    sent = []
    state = {"failed": False}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, text):
            sent.append(text)
            if text == "two" and not state["failed"]:
                state["failed"] = True
                raise TelegramError("temporary outage", category="transient")

    monkeypatch.setattr(telegram_delivery, "TelegramClient", Client)
    try:
        row = _enqueue(db, "2026-10-10", ["one", "two", "three"], NOW)
        with pytest.raises(TelegramError, match="temporary outage"):
            telegram_delivery.deliver_pending(db, _configured(settings), logging.getLogger("test"), now=NOW)

        pending = db.active_telegram_delivery("2026-10-10")
        assert pending["next_chunk"] == 1
        assert pending["attempt_count"] == 1

        result = telegram_delivery.deliver_pending(
            db, _configured(settings), logging.getLogger("test"), now=NOW + timedelta(minutes=30)
        )

        assert sent[0:2] == ["one", "two"]
        assert "Delayed delivery" in sent[2]
        assert sent[3:] == ["two", "three"]
        assert sent.count("one") == 1
        delivered = db.telegram_deliveries_with_status("delivered")[0]
        assert delivered["id"] == row["id"]
        assert delivered["delayed_notice_sent"] == 1
        assert db.telegram_delivery_chunks(delivered) == []
        assert result.completed_deliveries == 1
    finally:
        db.close()


def test_permanent_failure_pauses_backlog_and_notifies_once(monkeypatch, settings):
    db = Database(settings.database_file)
    notifications = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, text):
            raise TelegramError("credentials rejected", category="credentials", status_code=401)

    monkeypatch.setattr(telegram_delivery, "TelegramClient", Client)
    monkeypatch.setattr(telegram_delivery, "launch_tray_notification", lambda logger: notifications.append(True) or True)
    try:
        _enqueue(db, "2026-10-09", ["older"], NOW - timedelta(days=1))
        _enqueue(db, "2026-10-10", ["newer"], NOW)

        result = telegram_delivery.deliver_pending(
            db, _configured(settings), logging.getLogger("test"), now=NOW
        )

        rows = db.telegram_deliveries_with_status("needs_attention")
        assert len(rows) == 2
        assert all(row["last_error_category"] == "credentials" for row in rows)
        assert all(row["notified_at"] for row in rows)
        assert notifications == [True]
        assert result.needs_attention == 2
    finally:
        db.close()


def test_content_rejection_attempts_plain_fallback_once(monkeypatch, settings):
    db = Database(settings.database_file)
    plain = []
    notifications = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, text):
            raise TelegramError("request rejected", category="content", status_code=400)

        def send_plain_message(self, text):
            plain.append(text)

    monkeypatch.setattr(telegram_delivery, "TelegramClient", Client)
    monkeypatch.setattr(telegram_delivery, "launch_tray_notification", lambda logger: notifications.append(True) or True)
    try:
        _enqueue(db, "2026-10-10", ["formatted"], NOW)
        result = telegram_delivery.deliver_pending(
            db, _configured(settings), logging.getLogger("test"), now=NOW
        )

        row = db.telegram_deliveries_with_status("needs_attention")[0]
        assert row["fallback_notice_attempted"] == 1
        assert len(plain) == 1
        assert "outlook-triage telegram-retry" in plain[0]
        assert notifications == [True]
        assert result.needs_attention == 1
    finally:
        db.close()


def test_manual_reactivation_and_seven_day_expiry(monkeypatch, settings):
    db = Database(settings.database_file)
    sent = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, text):
            sent.append(text)

    monkeypatch.setattr(telegram_delivery, "TelegramClient", Client)
    try:
        old = _enqueue(db, "2026-10-01", ["expired"], NOW - timedelta(days=8))
        current = _enqueue(db, "2026-10-10", ["retry me"], NOW)
        db.mark_telegram_delivery_error(
            int(current["id"]),
            status="needs_attention",
            category="chat",
            message="chat not found",
            attempted_at=NOW.isoformat(),
        )

        assert db.reactivate_telegram_deliveries() == 1
        result = telegram_delivery.deliver_pending(
            db, _configured(settings), logging.getLogger("test"), now=NOW
        )

        assert sent == ["retry me"]
        expired = db.telegram_deliveries_with_status("expired")[0]
        assert expired["id"] == old["id"]
        assert db.telegram_delivery_chunks(expired) == []
        assert db.telegram_deliveries_with_status("delivered")[0]["id"] == current["id"]
        assert result.expired == 1
    finally:
        db.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows tray notification is Windows-only")
def test_tray_notification_is_nonblocking_and_contains_only_sanitized_guidance(monkeypatch):
    calls = []

    def popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return object()

    monkeypatch.setattr(telegram_delivery.shutil, "which", lambda name: r"C:\Windows\powershell.exe")
    monkeypatch.setattr(telegram_delivery.subprocess, "Popen", popen)

    assert telegram_delivery.launch_tray_notification(logging.getLogger("test")) is True
    arguments, kwargs = calls[0]
    joined = " ".join(arguments)
    assert "outlook-triage telegram-retry" in joined
    assert "bot-token" not in joined
    assert "digest contents" not in joined
    assert kwargs["stdout"] is telegram_delivery.subprocess.DEVNULL
    assert kwargs["stderr"] is telegram_delivery.subprocess.DEVNULL

