from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .filters import sender_details
from .models import EmailClassification


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS emails (
    message_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    conversation_id TEXT,
    sender_name TEXT NOT NULL DEFAULT '', sender_address TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL, importance TEXT,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    processing_status TEXT NOT NULL CHECK(processing_status IN ('pending','processed','skipped','failed')),
    skip_reason TEXT, requires_action INTEGER, urgency TEXT, action_type TEXT, task TEXT,
    deadline TEXT, deadline_raw TEXT, summary TEXT, category TEXT, reason TEXT,
    confidence REAL, model TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, processed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(source_id, store_id)
);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(processing_status);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_message_key TEXT NOT NULL UNIQUE REFERENCES emails(message_key),
    description TEXT NOT NULL, deadline TEXT, urgency TEXT NOT NULL, action_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','dismissed','waiting')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE TABLE IF NOT EXISTS sync_state (
    mailbox TEXT PRIMARY KEY, last_received_at TEXT NOT NULL, last_success_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_type TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL CHECK(status IN ('running','success','failed')),
    processed_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0, error_summary TEXT
);
CREATE TABLE IF NOT EXISTS telegram_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    chunks_json TEXT NOT NULL,
    next_chunk INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','needs_attention','delivered','expired')),
    last_attempt_at TEXT,
    last_error_category TEXT,
    last_error TEXT,
    notified_at TEXT,
    delayed_notice_sent INTEGER NOT NULL DEFAULT 0,
    fallback_notice_attempted INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_telegram_deliveries_status_created
    ON telegram_deliveries(status, created_at, id);
"""


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def start_run(self, run_type: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(run_type, started_at, status) VALUES (?, ?, 'running')", (run_type, local_now())
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, success: bool, processed: int = 0, skipped: int = 0,
                   failed: int = 0, error: str | None = None) -> None:
        self.connection.execute(
            """UPDATE runs SET finished_at=?, status=?, processed_count=?, skipped_count=?,
               failed_count=?, error_summary=? WHERE id=?""",
            (local_now(), "success" if success else "failed", processed, skipped, failed, error, run_id),
        )
        self.connection.commit()

    def get_last_received_at(self, mailbox: str = "inbox") -> str | None:
        row = self.connection.execute(
            "SELECT last_received_at FROM sync_state WHERE mailbox=?", (mailbox,)
        ).fetchone()
        return str(row["last_received_at"]) if row else None

    def store_sync_batch(self, messages: list[dict], last_received_at: str, mailbox: str = "inbox") -> None:
        now = local_now()
        with self.transaction() as conn:
            for message in messages:
                sender_name, sender_address = sender_details(message)
                conn.execute(
                    """INSERT INTO emails(
                           message_key, source_id, store_id, conversation_id, sender_name,
                           sender_address, subject, received_at, importance, has_attachments,
                           processing_status, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                       ON CONFLICT(message_key) DO UPDATE SET
                           conversation_id=excluded.conversation_id, sender_name=excluded.sender_name,
                           sender_address=excluded.sender_address, subject=excluded.subject,
                           importance=excluded.importance, has_attachments=excluded.has_attachments,
                           updated_at=excluded.updated_at""",
                    (message["messageKey"], message["sourceId"], message["storeId"],
                     message.get("conversationId"), sender_name, sender_address,
                     message.get("subject") or "(no subject)", message.get("receivedDateTime") or now,
                     message.get("importance"), int(bool(message.get("hasAttachments"))), now, now),
                )
            conn.execute(
                """INSERT INTO sync_state(mailbox, last_received_at, last_success_at) VALUES (?, ?, ?)
                   ON CONFLICT(mailbox) DO UPDATE SET last_received_at=excluded.last_received_at,
                   last_success_at=excluded.last_success_at""",
                (mailbox, last_received_at, now),
            )

    def email_status(self, message_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT processing_status FROM emails WHERE message_key=?", (message_key,)
        ).fetchone()
        return str(row["processing_status"]) if row else None

    def mark_skipped(self, message_key: str, reason: str) -> None:
        self.connection.execute(
            """UPDATE emails SET processing_status='skipped', skip_reason=?, last_error=NULL,
               processed_at=?, updated_at=? WHERE message_key=?""",
            (reason, local_now(), local_now(), message_key),
        )
        self.connection.commit()

    def mark_failed(self, message_key: str, error: str) -> None:
        self.connection.execute(
            """UPDATE emails SET processing_status='failed', attempts=attempts+1,
               last_error=?, updated_at=? WHERE message_key=?""",
            (error[:1000], local_now(), message_key),
        )
        self.connection.commit()

    def save_classification(self, message_key: str, classification: EmailClassification, model: str) -> None:
        now = local_now()
        data = classification.model_dump(mode="json")
        with self.transaction() as conn:
            conn.execute(
                """UPDATE emails SET processing_status='processed', requires_action=?, urgency=?,
                   action_type=?, task=?, deadline=?, deadline_raw=?, summary=?, category=?, reason=?,
                   confidence=?, model=?, attempts=attempts+1, last_error=NULL, processed_at=?, updated_at=?
                   WHERE message_key=?""",
                (int(classification.requires_action), classification.urgency, classification.action_type,
                 classification.task, data.get("deadline"), classification.deadline_raw,
                 classification.summary, classification.category, classification.reason,
                 classification.confidence, model, now, now, message_key),
            )
            if classification.requires_action and classification.task:
                conn.execute(
                    """INSERT INTO tasks(email_message_key, description, deadline, urgency, action_type,
                           status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                       ON CONFLICT(email_message_key) DO NOTHING""",
                    (message_key, classification.task, data.get("deadline"), classification.urgency,
                     classification.action_type, now, now),
                )

    def pending_messages(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT message_key, source_id, store_id FROM emails
               WHERE processing_status IN ('pending','failed') ORDER BY received_at ASC LIMIT ?""", (limit,)
        ).fetchall()

    def list_tasks(self, status: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE tasks.status=?" if status else ""
        params = (status,) if status else ()
        return self.connection.execute(
            f"""SELECT tasks.*, emails.subject, emails.sender_name, emails.sender_address
                FROM tasks JOIN emails ON emails.message_key=tasks.email_message_key {where}
                ORDER BY tasks.status, COALESCE(tasks.deadline, '9999-12-31'), tasks.id""", params
        ).fetchall()

    def set_task_status(self, task_id: int, status: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, local_now(), task_id)
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def task_email(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT tasks.id AS task_id, tasks.status AS task_status,
                      emails.source_id, emails.store_id, emails.subject
               FROM tasks JOIN emails ON emails.message_key=tasks.email_message_key
               WHERE tasks.id=?""",
            (task_id,),
        ).fetchone()

    def digest_snapshot(self, since: str) -> dict:
        counts = self.connection.execute(
            """SELECT
                 SUM(CASE WHEN category='information' THEN 1 ELSE 0 END) AS information_count,
                 SUM(CASE WHEN category='newsletter' THEN 1 ELSE 0 END) AS newsletter_count,
                 SUM(CASE WHEN category='automated' THEN 1 ELSE 0 END) AS automated_count,
                 SUM(CASE WHEN processing_status='skipped' THEN 1 ELSE 0 END) AS skipped_count,
                 SUM(CASE WHEN processing_status IN ('pending','failed') THEN 1 ELSE 0 END) AS failed_count
               FROM emails WHERE received_at >= ?""", (since,)
        ).fetchone()
        sync = self.connection.execute(
            "SELECT last_success_at FROM sync_state WHERE mailbox='inbox'"
        ).fetchone()
        last_run = self.connection.execute(
            "SELECT status, error_summary, finished_at FROM runs WHERE run_type='sync' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {"tasks": self.list_tasks(), "counts": dict(counts) if counts else {},
                "last_sync": sync["last_success_at"] if sync else None,
                "last_run": dict(last_run) if last_run else None}

    def active_telegram_delivery(self, digest_date: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM telegram_deliveries
               WHERE digest_date=? AND status IN ('pending','needs_attention')
               ORDER BY id ASC LIMIT 1""",
            (digest_date,),
        ).fetchone()

    def enqueue_telegram_delivery(self, digest_date: str, chunks: list[str], created_at: str) -> sqlite3.Row:
        with self.transaction() as conn:
            existing = conn.execute(
                """SELECT * FROM telegram_deliveries
                   WHERE digest_date=? AND status IN ('pending','needs_attention')
                   ORDER BY id ASC LIMIT 1""",
                (digest_date,),
            ).fetchone()
            if existing:
                return existing
            cursor = conn.execute(
                """INSERT INTO telegram_deliveries(digest_date, created_at, chunks_json)
                   VALUES (?, ?, ?)""",
                (digest_date, created_at, json.dumps(chunks, ensure_ascii=False)),
            )
            delivery_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM telegram_deliveries WHERE id=?", (delivery_id,)).fetchone()
            assert row is not None
            return row

    @staticmethod
    def telegram_delivery_chunks(row: sqlite3.Row) -> list[str]:
        value = json.loads(str(row["chunks_json"]))
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("Stored Telegram delivery has invalid chunks")
        return value

    def expire_telegram_deliveries(self, created_before: str) -> int:
        cursor = self.connection.execute(
            """UPDATE telegram_deliveries SET status='expired', chunks_json='[]'
               WHERE status IN ('pending','needs_attention') AND created_at < ?""",
            (created_before,),
        )
        self.connection.commit()
        return cursor.rowcount

    def pending_telegram_deliveries(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT * FROM telegram_deliveries WHERE status='pending'
               ORDER BY created_at ASC, id ASC"""
        ).fetchall()

    def telegram_deliveries_with_status(self, status: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM telegram_deliveries WHERE status=? ORDER BY created_at ASC, id ASC",
            (status,),
        ).fetchall()

    def start_telegram_attempt(self, delivery_id: int, attempted_at: str) -> sqlite3.Row:
        self.connection.execute(
            """UPDATE telegram_deliveries
               SET attempt_count=attempt_count+1, last_attempt_at=? WHERE id=?""",
            (attempted_at, delivery_id),
        )
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM telegram_deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(f"Telegram delivery {delivery_id} was not found")
        return row

    def mark_telegram_chunk_sent(self, delivery_id: int, next_chunk: int, total_chunks: int, sent_at: str) -> None:
        delivered = next_chunk >= total_chunks
        self.connection.execute(
            """UPDATE telegram_deliveries
               SET next_chunk=?, status=?, delivered_at=?, last_error_category=NULL, last_error=NULL,
                   chunks_json=CASE WHEN ? THEN '[]' ELSE chunks_json END
               WHERE id=?""",
            (
                next_chunk,
                "delivered" if delivered else "pending",
                sent_at if delivered else None,
                int(delivered),
                delivery_id,
            ),
        )
        self.connection.commit()

    def mark_telegram_delayed_notice_sent(self, delivery_id: int) -> None:
        self.connection.execute(
            "UPDATE telegram_deliveries SET delayed_notice_sent=1 WHERE id=?", (delivery_id,)
        )
        self.connection.commit()

    def mark_telegram_fallback_attempted(self, delivery_id: int) -> None:
        self.connection.execute(
            "UPDATE telegram_deliveries SET fallback_notice_attempted=1 WHERE id=?", (delivery_id,)
        )
        self.connection.commit()

    def mark_telegram_delivery_error(
        self, delivery_id: int, *, status: str, category: str, message: str, attempted_at: str
    ) -> None:
        self.connection.execute(
            """UPDATE telegram_deliveries
               SET status=?, last_attempt_at=?, last_error_category=?, last_error=? WHERE id=?""",
            (status, attempted_at, category, message[:500], delivery_id),
        )
        self.connection.commit()

    def mark_telegram_notification_sent(self, delivery_ids: list[int], notified_at: str) -> None:
        if not delivery_ids:
            return
        placeholders = ",".join("?" for _ in delivery_ids)
        self.connection.execute(
            f"UPDATE telegram_deliveries SET notified_at=? WHERE id IN ({placeholders})",
            (notified_at, *delivery_ids),
        )
        self.connection.commit()

    def reactivate_telegram_deliveries(self) -> int:
        cursor = self.connection.execute(
            """UPDATE telegram_deliveries
               SET status='pending', notified_at=NULL, last_error_category=NULL, last_error=NULL
               WHERE status='needs_attention'"""
        )
        self.connection.commit()
        return cursor.rowcount

    def export_debug_schema(self) -> dict:
        result = {}
        for table in ("emails", "tasks", "sync_state", "runs", "telegram_deliveries"):
            rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
            result[table] = [row["name"] for row in rows]
        return json.loads(json.dumps(result))

