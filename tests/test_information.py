import logging
import sqlite3
from datetime import datetime, timedelta

import pytest

from outlook_triage.database import Database, SCHEMA
from outlook_triage.digest import build_digest
from outlook_triage.filters import FilterRules
from outlook_triage.models import EmailClassification
from outlook_triage.outlook import OutlookError, SyncResult
from outlook_triage.service import synchronize
from outlook_triage.telegram import format_digest
from outlook_triage.telegram_commands import parse_command
from outlook_triage.telegram_inbound import process_command


def add_information(db, now, source="info", *, is_read=False, category="information", action=False):
    message = {"messageKey": source, "sourceId": source, "storeId": "store", "subject": "<FYI>",
               "receivedDateTime": now.isoformat(), "isRead": is_read}
    db.store_sync_batch([message], now.isoformat())
    db.save_classification(source, EmailClassification(
        requires_action=action, urgency="normal" if action else "none",
        action_type="reply" if action else "none", task="Reply" if action else None,
        summary="A saved <summary> & detail", category=category, reason="FYI", confidence=1,
    ), "model")
    return message


def test_information_window_order_read_status_and_stable_upgrade(settings):
    now = datetime.now(settings.timezone).replace(microsecond=0)
    db = Database(settings.database_file)
    add_information(db, now - timedelta(hours=71), "old")
    add_information(db, now, "new")
    add_information(db, now - timedelta(hours=72), "boundary")
    add_information(db, now + timedelta(seconds=1), "future")
    add_information(db, now, "read", is_read=True)
    add_information(db, now, "newsletter", category="newsletter")
    add_information(db, now, "action", action=True)
    add_information(db, now, "unknown")
    db.connection.execute("UPDATE emails SET is_read=NULL WHERE message_key='unknown'")
    db.connection.commit()
    rows = db.information(now, unread_only=True)
    assert [row["source_id"] for row in rows] == ["new", "old"]
    references = [(row["source_id"], row["id"]) for row in db.information(now)]
    db.close()
    db = Database(settings.database_file)
    assert [(row["source_id"], row["id"]) for row in db.information(now)] == references
    assert len(db.list_tasks()) == 1
    db.close()


def test_pre_upgrade_database_backfills_unknown_read_state(settings):
    settings.database_file.parent.mkdir(parents=True)
    connection = sqlite3.connect(settings.database_file)
    connection.executescript(SCHEMA)
    connection.execute("""INSERT INTO emails(message_key,source_id,store_id,received_at,
        processing_status,requires_action,category,summary,created_at,updated_at)
        VALUES('old','old','store',?,'processed',0,'information','Saved summary',?,?)""", ("2026-11-09T00:00:00Z",) * 3)
    connection.commit()
    connection.close()
    db = Database(settings.database_file)
    assert db.information_email(1)["is_read"] is None
    db.close()
    db = Database(settings.database_file)
    assert db.connection.execute("SELECT COUNT(*) FROM information_references").fetchone()[0] == 1
    db.close()


def test_information_digest_escapes_and_has_no_task_controls(settings):
    db = Database(settings.database_file)
    now = datetime.now(settings.timezone)
    add_information(db, now)
    digest = build_digest(db, settings, now)
    assert digest.index("## Waiting / follow-up") < digest.index("## Informational emails") < digest.index("## Last 24 hours")
    assert "/show #i1" in digest and "/read #i1" in digest
    rendered = "\n".join(format_digest(digest))
    assert "&lt;summary&gt; &amp; detail" in rendered
    assert "☐ #i" not in rendered
    db.set_email_read("info", True)
    assert "/show #i1" not in build_digest(db, settings, now)
    db.close()


def test_information_chunks_preserve_long_content_and_escaped_size(settings):
    db = Database(settings.database_file)
    now = datetime.now(settings.timezone)
    add_information(db, now)
    db.connection.execute("UPDATE emails SET subject=?, summary=?", ("'" * 4000, "&" * 1000))
    db.connection.commit()
    chunks = format_digest(build_digest(db, settings, now))
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks).count("&amp;") == 1000
    assert "".join(chunks).count("&#x27;") == 4000
    db.close()


def test_information_window_compares_timezone_instants(settings):
    db = Database(settings.database_file)
    now = datetime.fromisoformat("2026-11-09T08:00:00+08:00")
    add_information(db, datetime.fromisoformat("2026-11-06T00:00:00+00:00"), "boundary")
    add_information(db, datetime.fromisoformat("2026-11-06T00:00:01+00:00"), "inside")
    assert [row["source_id"] for row in db.information(now, unread_only=True)] == ["inside"]
    db.close()


@pytest.mark.parametrize("text", ["/show #i1", "/show@bot I1", "/read #i1 #I2 #i1"])
def test_informational_command_namespace(text):
    command = parse_command(text)
    assert command.information_ids
    assert command.task_ids == ()


@pytest.mark.parametrize("text", ["/read #1 #i1", "/done #i1", "/show #i1 #i2", "/read #i0", "/waiting i1", "/read i9007199254740992"])
def test_information_rejects_invalid_commands(text):
    assert parse_command(text) is None


@pytest.mark.parametrize("fail", [False, True])
def test_sync_reconciles_old_processed_information_without_classification(settings, fail):
    db = Database(settings.database_file)
    now = datetime.now(settings.timezone)
    add_information(db, now - timedelta(hours=60))
    class Outlook:
        def sync(self, *args, **kwargs): return SyncResult([], now.isoformat())
        def is_read(self, source, store):
            assert (source, store) == ("info", "store")
            if fail: raise OutlookError("unavailable")
            return True
    class AI:
        def verify_model(self): pass
        def classify(self, *args): raise AssertionError("Must reuse classification")
    synchronize(db=db, settings=settings, outlook=Outlook(), ai=AI(), rules=FilterRules(), logger=logging.getLogger())
    assert db.information_email(1)["is_read"] == (0 if fail else 1)
    assert db.digest_snapshot(now.isoformat())["last_run"]["status"] == ("failed" if fail else "success")
    db.close()


def test_local_show_and_partial_read_leave_tasks_unchanged(settings):
    db = Database(settings.database_file)
    now = datetime.now(settings.timezone)
    add_information(db, now)
    add_information(db, now, "missing")
    add_information(db, now, "task", category="action", action=True)
    class Outlook:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_body_preview(self, *args, **kwargs): return "Original <body>"
        def mark_read(self, source, store):
            if source == "missing": raise OutlookError("missing")
            return False  # Already read in Outlook is still a success.
    class Telegram:
        def __init__(self): self.messages = []
        def send_message(self, text): self.messages.append(text)
        send_plain_message = send_message
    telegram = Telegram()
    process_command(db, settings, telegram, 1, "1", "/show #i1", logging.getLogger(), outlook_factory=Outlook)
    assert "Original &lt;body&gt;" in "".join(telegram.messages)
    assert db.information_email(1)["is_read"] == 0
    process_command(db, settings, telegram, 2, "1", "/read #i1 #i2", logging.getLogger(), outlook_factory=Outlook)
    assert db.information_email(1)["is_read"] == 1
    assert db.information_email(2)["is_read"] == 0
    assert db.list_tasks()[0]["status"] == "open"
    assert "Earlier successes were kept" in telegram.messages[-1]
    db.close()
