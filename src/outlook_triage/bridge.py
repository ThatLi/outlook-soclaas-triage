"""Outbound-only Windows mailbox bridge for the cloud-owned Telegram bot.

Cloud task state wins over the local mirror. Local receipts survive a lost cloud
acknowledgement so completed syncs/classifications are not deliberately repeated.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .database import Database
from .locking import AlreadyRunning, process_lock
from .operations import run_retry, run_sync
from .outlook import OutlookClient, OutlookError
from .relay import RelayClient, RelayError, build_payload, encrypt_payload, relay_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bridge_receipts(
    event_id TEXT NOT NULL, item_id INTEGER NOT NULL,
    result_json TEXT NOT NULL, PRIMARY KEY(event_id,item_id)
);
"""


def initialize_bridge(db: Database) -> str:
    db.connection.executescript(SCHEMA)
    db.connection.execute("INSERT OR IGNORE INTO bridge_state(key,value) VALUES('dataset_id',?)", (str(uuid.uuid4()),))
    db.connection.commit()
    return str(db.connection.execute("SELECT value FROM bridge_state WHERE key='dataset_id'").fetchone()[0])


class BridgeClient(RelayClient):
    def __init__(self, settings: Settings, dataset_id: str, **kwargs):
        super().__init__(settings, **kwargs)
        self.dataset_id = dataset_id

    def poll(self):
        return self._request("GET", "/v2/bridge/poll", headers={"X-Bridge-Dataset": self.dataset_id})

    def acknowledge(self, event_id: int, **result):
        return self._request("POST", "/v2/bridge/ack", headers={"X-Bridge-Dataset": self.dataset_id},
                             json={"event_id": event_id, **result})

    def publish(self, envelope):
        return self._request("POST", "/v2/snapshot", json=envelope)


def publish_bridge(db: Database, settings: Settings, client: BridgeClient, *, cache_previews: bool = False,
                   outlook_factory=OutlookClient) -> None:
    db.connection.execute("INSERT OR REPLACE INTO bridge_state VALUES('publish_pending','1')")
    db.connection.commit()
    now = datetime.now(settings.timezone)
    envelope, payload = build_payload(db, settings, now)
    # V2 renders from cloud task states. Do not upload duplicate pre-rendered
    # views for every status: small envelopes help stay within Workers Free CPU.
    payload["digest_chunks"] = []
    payload["cached_commands"] = {"list": [], "status": [], "tasks": {
        status: [] for status in ("all", "open", "waiting", "done", "dismissed")
    }}
    tasks = [{name: row[name] for name in (
        "id", "status", "description", "deadline", "urgency", "sender_name", "sender_address", "subject"
    )} for row in db.list_tasks()]
    if cache_previews and tasks:
        with process_lock(settings.lock_dir):
            with outlook_factory(settings.outlook_profile, timezone=settings.timezone) as outlook:
                for task in tasks:
                    row = db.task_email(task["id"])
                    try:
                        task["preview"] = outlook.get_body_preview(
                            row["source_id"], row["store_id"], limit=min(12000, settings.telegram_body_preview_chars),
                        )
                    except OutlookError:
                        # Missing/moved mail must not stop publication of the other tasks.
                        continue
    snapshot = db.digest_snapshot((now - timedelta(hours=24)).isoformat(timespec="seconds"))
    payload["bridge"] = {
        "dataset_id": client.dataset_id, "tasks": tasks,
        "horizon_days": settings.digest_horizon_days,
        "health": db.operational_status(), "counts": snapshot["counts"],
        "processed_telegram_updates": [row[0] for row in db.connection.execute(
            "SELECT update_id FROM telegram_updates WHERE status IN ('applied','responded','failed') ORDER BY update_id DESC LIMIT 1000"
        )],
    }
    client.publish(encrypt_payload(envelope, payload, relay_key(settings)))
    db.connection.execute("DELETE FROM bridge_state WHERE key='publish_pending'")
    db.connection.commit()


def _mirror(db: Database, tasks: list[dict]) -> None:
    with db.transaction() as conn:
        for task in tasks:
            if task.get("status") not in {"open", "waiting", "done", "dismissed"}:
                raise RelayError("Invalid cloud task state")
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (task["status"], task["id"]))


def _receipt(db: Database, event: str, item: int):
    row = db.connection.execute("SELECT result_json FROM bridge_receipts WHERE event_id=? AND item_id=?", (event, item)).fetchone()
    return json.loads(row[0]) if row else None


def _save_receipt(db: Database, event: str, item: int, result: dict) -> None:
    db.connection.execute("INSERT OR IGNORE INTO bridge_receipts VALUES(?,?,?)", (event, item, json.dumps(result)))
    db.connection.commit()


def process_job(db: Database, settings: Settings, client: BridgeClient, job: dict, logger: logging.Logger,
                *, cache_previews: bool = False, outlook_factory=OutlookClient,
                sync: Callable = run_sync, retry: Callable = run_retry) -> None:
    event_id = int(job["id"])
    # Telegram update keys survive cloud database recreation; numeric row IDs do not.
    receipt_key = str(job.get("dedupe_key", f"legacy:{event_id}"))
    try:
        if job["action"] in {"sync", "list", "retry"}:
            counts = _receipt(db, receipt_key, 0)
            if counts is None:
                counts = (retry(db, settings, logger, limit=int(job["retry_limit"]))
                          if job["action"] == "retry" else sync(db, settings, logger))
                _save_receipt(db, receipt_key, 0, counts)
            publish_bridge(db, settings, client, cache_previews=cache_previews, outlook_factory=outlook_factory)
            client.acknowledge(event_id, counts=counts)
        elif job["action"] in {"read", "done", "dismiss"}:
            ids = [int(value) for value in job["task_ids"]]
            if not ids or len(ids) > 100:
                raise OutlookError("Invalid queued task list")
            rows = {task_id: db.task_email(task_id) for task_id in ids}
            if any(row is None for row in rows.values()):
                raise OutlookError("A queued task is missing locally")
            with process_lock(settings.lock_dir):
                with outlook_factory(settings.outlook_profile, timezone=settings.timezone) as outlook:
                    for task_id in ids:
                        if task_id in job["completed_ids"]:
                            continue
                        if _receipt(db, receipt_key, task_id) is None:
                            row = rows[task_id]
                            outlook.mark_read(row["source_id"], row["store_id"])
                            _save_receipt(db, receipt_key, task_id, {"read": True})
                        # Cloud changes done/dismissed only AFTER this acknowledgement.
                        client.acknowledge(event_id, task_id=task_id)
        else:
            raise OutlookError("Unsupported bridge operation")
    except (RelayError, AlreadyRunning):
        # A lost response or contention leaves the cloud job pending for retry.
        raise
    except Exception as exc:
        logger.warning("Cloud bridge operation failed (%s)", type(exc).__name__)
        publish_bridge(db, settings, client, cache_previews=cache_previews, outlook_factory=outlook_factory)
        client.acknowledge(event_id, failed=True)


def bridge_cycle(db: Database, settings: Settings, logger: logging.Logger, *, client: BridgeClient | None = None,
                 cache_previews: bool = False, now: datetime | None = None, sync: Callable = run_sync,
                 outlook_factory=OutlookClient) -> int:
    dataset_id = initialize_bridge(db)
    client = client or BridgeClient(settings, dataset_id)
    try:
        state = client.poll()
    except RelayError as exc:
        if exc.status_code != 409:
            raise
        publish_bridge(db, settings, client, cache_previews=cache_previews, outlook_factory=outlook_factory)
        state = client.poll()
    _mirror(db, state["tasks"])
    if db.connection.execute("SELECT value FROM bridge_state WHERE key='publish_pending'").fetchone():
        publish_bridge(db, settings, client, cache_previews=cache_previews, outlook_factory=outlook_factory)
    processed = 0
    # Bound each pass. Poll again after each job to see later cloud task changes.
    for _ in range(10):
        job = state.get("job")
        if not job:
            break
        process_job(db, settings, client, job, logger, cache_previews=cache_previews,
                    sync=sync, outlook_factory=outlook_factory)
        state = client.poll()
        _mirror(db, state["tasks"])
        processed += 1
    current = (now or datetime.now(ZoneInfo("Asia/Singapore"))).astimezone(ZoneInfo("Asia/Singapore"))
    slot = f"{current.date().isoformat()}:{current.hour // 4}"
    previous = db.connection.execute("SELECT value FROM bridge_state WHERE key='sync_slot'").fetchone()
    if not previous or previous[0] != slot:
        # One attempt per four-hour slot, including catch-up on logon. /sync is independent.
        try:
            sync(db, settings, logger)
        except AlreadyRunning:
            raise
        except Exception as exc:
            logger.warning("Scheduled bridge synchronization failed (%s)", type(exc).__name__)
        db.connection.execute("INSERT OR REPLACE INTO bridge_state VALUES('sync_slot',?)", (slot,))
        db.connection.commit()
        publish_bridge(db, settings, client, cache_previews=cache_previews, outlook_factory=outlook_factory)
    elif processed:
        publish_bridge(db, settings, client, cache_previews=cache_previews, outlook_factory=outlook_factory)
    return processed


def listen_bridge(db: Database, settings: Settings, logger: logging.Logger, *, cache_previews: bool = False,
                  sleep: Callable = time.sleep, max_cycles: int | None = None) -> None:
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        os.utime(settings.state_dir / "cloud-bridge.lock", None)
        try:
            bridge_cycle(db, settings, logger, cache_previews=cache_previews)
        except (RelayError, AlreadyRunning) as exc:
            logger.warning("Cloud bridge will retry (%s)", type(exc).__name__)
        sleep(30)
