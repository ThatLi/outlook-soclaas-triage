import base64
import logging
from dataclasses import replace
from datetime import datetime

import pytest

from outlook_triage.bridge import bridge_cycle, initialize_bridge, process_job, publish_bridge
from outlook_triage.database import Database
from outlook_triage.models import EmailClassification
from outlook_triage.outlook import OutlookError
from outlook_triage.relay import RelayError, decrypt_payload


# Public, deterministic test vector; never a deployed encryption credential.
TEST_ENCRYPTION_KEY = bytes(range(32))


class Client:
    def __init__(self, db):
        self.dataset_id = initialize_bridge(db)
        self.acks = []
        self.envelopes = []
        self.state = {"tasks": [], "job": None}
        self.fail_ack_once = False

    def poll(self):
        return self.state

    def publish(self, envelope):
        self.envelopes.append(envelope)

    def acknowledge(self, event_id, **result):
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RelayError("lost response")
        self.acks.append((event_id, result))


@pytest.fixture
def bridge(settings):
    config = replace(settings, relay_url="https://cloud.test", relay_token="token",
                     relay_encryption_key=base64.urlsafe_b64encode(TEST_ENCRYPTION_KEY).rstrip(b"=").decode("ascii"),
                     cloud_bot_enabled=True)
    db = Database(config.database_file)
    initialize_bridge(db)
    for number in (1, 2):
        message = {"messageKey": f"store:entry{number}", "sourceId": f"entry{number}", "storeId": "store",
                   "subject": f"subject {number}", "receivedDateTime": "2026-10-21T00:00:00+08:00",
                   "from": {"emailAddress": {"name": "Sender", "address": "sender@example.test"}}}
        db.store_sync_batch([message], message["receivedDateTime"])
        db.save_classification(message["messageKey"], EmailClassification(
            requires_action=True, urgency="normal", action_type="reply", task=f"task {number}",
            summary="summary", category="action", reason="reason", confidence=1,
        ), "model")
    yield db, config, Client(db)
    db.close()


def test_sync_receipt_survives_lost_cloud_ack(bridge):
    db, config, client = bridge
    calls = []
    job = {"id": 1, "action": "list", "retry_limit": 50, "task_ids": [], "completed_ids": []}
    def sync(*args):
        calls.append(True)
        return {"processed": 2, "failed": 0}
    client.fail_ack_once = True
    with pytest.raises(RelayError):
        process_job(db, config, client, job, logging.getLogger(), sync=sync)
    process_job(db, config, client, job, logging.getLogger(), sync=sync)
    assert len(calls) == 1
    assert client.acks == [(1, {"counts": {"processed": 2, "failed": 0}})]


def test_read_receipt_survives_lost_ack_without_mutating_local_status(bridge):
    db, config, client = bridge
    reads = []
    class Outlook:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def mark_read(self, source, store): reads.append(source)
    job = {"id": 3, "action": "done", "task_ids": [1], "completed_ids": []}
    client.fail_ack_once = True
    with pytest.raises(RelayError):
        process_job(db, config, client, job, logging.getLogger(), outlook_factory=Outlook)
    assert db.task_email(1)["task_status"] == "open"
    process_job(db, config, client, job, logging.getLogger(), outlook_factory=Outlook)
    assert reads == ["entry1"]
    assert client.acks == [(3, {"task_id": 1})]


def test_partial_failure_and_full_batch_validation(bridge):
    db, config, client = bridge
    reads = []
    class Outlook:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def mark_read(self, source, store):
            if source == "entry2": raise OutlookError("unavailable")
            reads.append(source)
    job = {"id": 4, "action": "dismiss", "task_ids": [1, 2], "completed_ids": []}
    process_job(db, config, client, job, logging.getLogger(), outlook_factory=Outlook)
    assert client.acks == [(4, {"task_id": 1}), (4, {"failed": True})]
    process_job(db, config, client, {**job, "id": 5, "task_ids": [1, 999]}, logging.getLogger(), outlook_factory=Outlook)
    assert reads == ["entry1"]


def test_four_hour_slots_catch_up_and_mirror_cloud_state(bridge):
    db, config, client = bridge
    calls = []
    client.state["tasks"] = [{"id": 1, "status": "waiting"}]
    def sync(*args): calls.append(True)
    for hour in (1, 2, 3, 4, 4, 8):
        bridge_cycle(db, config, logging.getLogger(), client=client, sync=sync,
                     now=datetime(2026, 10, 21, hour, tzinfo=config.timezone))
    assert len(calls) == 3  # catch-up 01:00, then 04:00 and 08:00
    assert db.task_email(1)["task_status"] == "waiting"
    payload = decrypt_payload(client.envelopes[-1], TEST_ENCRYPTION_KEY)
    assert next(task for task in payload["bridge"]["tasks"] if task["id"] == 1)["status"] == "waiting"
    assert all("preview" not in task for task in payload["bridge"]["tasks"])


def test_previews_are_bounded_and_opt_in(bridge):
    db, config, client = bridge
    class Outlook:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_body_preview(self, source, store, limit): return ("private " * 5000)[:limit]
    publish_bridge(db, config, client, cache_previews=True, outlook_factory=Outlook)
    payload = decrypt_payload(client.envelopes[0], TEST_ENCRYPTION_KEY)
    assert len(payload["bridge"]["tasks"][0]["preview"]) == 6000
    assert "private " not in str(client.envelopes[0])


def test_failed_scheduled_publication_retries_without_resyncing(bridge):
    db, config, client = bridge
    calls = []
    def sync(*args): calls.append(True)
    original = client.publish
    def fail_once(envelope):
        client.publish = original
        raise RelayError("offline")
    client.publish = fail_once
    now = datetime(2026, 10, 21, 4, tzinfo=config.timezone)
    with pytest.raises(RelayError):
        bridge_cycle(db, config, logging.getLogger(), client=client, sync=sync, now=now)
    bridge_cycle(db, config, logging.getLogger(), client=client, sync=sync, now=now)
    assert len(calls) == 1
    assert len(client.envelopes) == 1


@pytest.mark.parametrize("command", ["sync", "retry-failed", "telegram-listen", "telegram-poll", "telegram-retry", "relay-publish", "digest --telegram", "tasks done 1"])
def test_cloud_mode_blocks_legacy_state_writers(settings, monkeypatch, command):
    from outlook_triage import cli
    from outlook_triage.config import ConfigurationError
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, cloud_bot_enabled=True))
    with pytest.raises(ConfigurationError, match="Cloud bot owns"):
        cli.run(cli.build_parser().parse_args(command.split()))


def test_bridge_lock_recovers_a_dead_process_but_keeps_a_live_owner(tmp_path):
    import os
    import subprocess
    import sys
    from outlook_triage.locking import AlreadyRunning, process_lock
    # Obtain a real short-lived process ID, not a guessed PID that could be alive.
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    lock = tmp_path / "bridge.lock"
    lock.mkdir()
    (lock / "owner").write_text(f"pid={process.pid}\n", encoding="utf-8")
    with process_lock(lock, recover_dead_owner=True):
        assert f"pid={os.getpid()}" in (lock / "owner").read_text()
        with pytest.raises(AlreadyRunning):
            with process_lock(lock, recover_dead_owner=True):
                pytest.fail("An active bridge owner was displaced")
