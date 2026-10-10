from datetime import datetime

from outlook_triage.database import Database
from outlook_triage.digest import build_digest
from outlook_triage.models import EmailClassification


def _message(source_id="entry-1"):
    return {
        "messageKey": f"store-1:{source_id}",
        "sourceId": source_id,
        "storeId": "store-1",
        "conversationId": "c1",
        "sender": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
        "subject": "Report needed",
        "receivedDateTime": "2026-09-28T00:00:00+08:00",
        "importance": "normal",
        "hasAttachments": True,
    }


def test_database_deduplicates_email_and_task(settings):
    db = Database(settings.database_file)
    try:
        db.store_sync_batch([_message()], "2026-09-28T08:00:00+08:00")
        db.store_sync_batch([_message()], "2026-09-28T09:00:00+08:00")
        classification = EmailClassification.model_validate(
            {
                "requires_action": True,
                "urgency": "urgent",
                "action_type": "submit",
                "task": "Submit the report",
                "deadline": "2026-09-28",
                "deadline_raw": "today",
                "summary": "A report is required today.",
                "category": "action",
                "reason": "Direct submission request.",
                "confidence": 0.95,
            }
        )
        db.save_classification("store-1:entry-1", classification, "test-model")
        db.save_classification("store-1:entry-1", classification, "test-model")
        assert len(db.list_tasks()) == 1
        task_email = db.task_email(1)
        assert task_email is not None
        assert (task_email["source_id"], task_email["store_id"], task_email["subject"]) == (
            "entry-1", "store-1", "Report needed"
        )
        assert db.get_last_received_at() == "2026-09-28T09:00:00+08:00"
        columns = db.export_debug_schema()["emails"]
        assert "body" not in columns
        assert "html" not in columns
        assert "source_id" in columns
        assert "store_id" in columns
        update_columns = db.export_debug_schema()["telegram_updates"]
        assert "body" not in update_columns
        assert "source_id" not in update_columns
        assert "command" in update_columns
    finally:
        db.close()


def test_digest_groups_open_task_and_reports_sync(settings):
    db = Database(settings.database_file)
    try:
        db.store_sync_batch([_message()], "2026-09-28T08:00:00+08:00")
        db.save_classification(
            "store-1:entry-1",
            EmailClassification.model_validate(
                {
                    "requires_action": True,
                    "urgency": "urgent",
                    "action_type": "submit",
                    "task": "Submit the report",
                    "deadline": "2026-09-28",
                    "deadline_raw": "today",
                    "summary": "Report needed.",
                    "category": "action",
                    "reason": "Explicit request.",
                    "confidence": 1,
                }
            ),
            "test-model",
        )
        text = build_digest(db, settings, datetime.fromisoformat("2026-09-28T08:00:00+08:00"))
        assert "Urgent / due today" in text
        assert "Submit the report" in text
        assert "Last successful Outlook sync:" in text
    finally:
        db.close()

