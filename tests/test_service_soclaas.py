import logging
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, APITimeoutError, RateLimitError

from outlook_triage.database import Database
from outlook_triage.filters import FilterRules
from outlook_triage.models import EmailClassification, EmailForClassification
from outlook_triage.outlook import SyncResult
from outlook_triage.outlook import OutlookUnavailable
from outlook_triage.service import synchronize
from outlook_triage.soclaas import SoCLaaSClient, SoCLaaSError


MESSAGE = {
    "messageKey": "store-1:entry-1",
    "sourceId": "entry-1",
    "storeId": "store-1",
    "conversationId": "conversation-1",
    "sender": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
    "subject": "Please reply",
    "receivedDateTime": "2026-09-28T01:00:00Z",
    "importance": "normal",
    "hasAttachments": False,
    "body": {"contentType": "text", "content": "Can you reply by tomorrow?"},
}


class FakeOutlook:
    def sync(self, last_received_at, *, bootstrap_days, overlap_hours):
        assert last_received_at is None
        assert bootstrap_days == 7
        assert overlap_hours == 8
        return SyncResult([MESSAGE], "2026-09-28T09:00:00+08:00")


class FakeAI:
    def __init__(self, fail=False):
        self.fail = fail
        self.verified = False

    def verify_model(self):
        self.verified = True

    def classify(self, email, now):
        assert "reply by tomorrow" in email.body
        if self.fail:
            raise SoCLaaSError("temporary failure")
        return EmailClassification.model_validate(
            {
                "requires_action": True,
                "urgency": "soon",
                "action_type": "reply",
                "task": "Reply to Alice",
                "deadline": "2026-09-29",
                "deadline_raw": "tomorrow",
                "summary": "Alice requested a reply.",
                "category": "action",
                "reason": "Direct reply request.",
                "confidence": 0.9,
            }
        )


def test_sync_persists_metadata_task_and_delta(settings):
    db = Database(settings.database_file)
    ai = FakeAI()
    try:
        counts = synchronize(
            db=db,
            outlook=FakeOutlook(),
            ai=ai,
            rules=FilterRules(),
            settings=settings,
            logger=logging.getLogger("test"),
        )
        assert ai.verified
        assert counts == {"processed": 1, "skipped": 0, "failed": 0, "unchanged": 0}
        assert db.get_last_received_at() == "2026-09-28T09:00:00+08:00"
        assert db.email_status("store-1:entry-1") == "processed"
        assert db.list_tasks()[0]["description"] == "Reply to Alice"
    finally:
        db.close()


def test_sync_keeps_failed_message_for_retry(settings):
    db = Database(settings.database_file)
    try:
        counts = synchronize(
            db=db,
            outlook=FakeOutlook(),
            ai=FakeAI(fail=True),
            rules=FilterRules(),
            settings=settings,
            logger=logging.getLogger("test"),
        )
        assert counts["failed"] == 1
        assert db.pending_messages()[0]["source_id"] == "entry-1"
        assert db.get_last_received_at() == "2026-09-28T09:00:00+08:00"
    finally:
        db.close()


def test_outlook_failure_does_not_advance_high_water_timestamp(settings):
    previous_high_water = "2026-09-27T09:00:00+08:00"

    class UnavailableOutlook:
        def sync(self, last_received_at, *, bootstrap_days, overlap_hours):
            assert last_received_at == previous_high_water
            raise OutlookUnavailable("Outlook temporarily unavailable")

    db = Database(settings.database_file)
    db.store_sync_batch([], previous_high_water)
    try:
        with pytest.raises(OutlookUnavailable, match="temporarily unavailable"):
            synchronize(
                db=db,
                outlook=UnavailableOutlook(),
                ai=FakeAI(),
                rules=FilterRules(),
                settings=settings,
                logger=logging.getLogger("test"),
            )
        assert db.get_last_received_at() == previous_high_water
        snapshot = db.digest_snapshot("2000-01-01T00:00:00+00:00")
        assert snapshot["last_run"]["status"] == "failed"
    finally:
        db.close()


def test_invalid_model_output_gets_one_repair_attempt(settings):
    client = object.__new__(SoCLaaSClient)
    client.settings = settings
    responses = iter(
        [
            "not json",
            '{"requires_action":false,"urgency":"none","action_type":"none","task":null,'
            '"deadline":null,"deadline_raw":null,"summary":"FYI only.",'
            '"category":"information","reason":"No request.","confidence":0.8}',
        ]
    )
    client._completion = lambda messages: next(responses)
    result = client.classify(
        EmailForClassification(
            source_id="id",
            sender="Alice",
            subject="FYI",
            received_at="2026-09-28T00:00:00+08:00",
            body="For your information.",
        ),
        datetime.fromisoformat("2026-09-28T08:00:00+08:00"),
    )
    assert result.category == "information"


class CompletionSequence:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=value))])


def _client_with_completions(settings, values, sleeps):
    client = object.__new__(SoCLaaSClient)
    client.settings = settings
    sequence = CompletionSequence(values)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=sequence))
    client.sleep = sleeps.append
    return client, sequence


def _status_error(status):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    if status == 429:
        return RateLimitError("limited", response=response, body=None)
    return APIStatusError("status error", response=response, body=None)


@pytest.mark.parametrize("status, expected", [(401, "API key"), (403, "model or feature")])
def test_non_retryable_soclaas_statuses_stop_immediately(settings, status, expected):
    sleeps = []
    client, sequence = _client_with_completions(settings, [_status_error(status)], sleeps)
    with pytest.raises(SoCLaaSError, match=expected):
        client._completion([{"role": "user", "content": "test"}])
    assert sequence.calls == 1
    assert sleeps == []


@pytest.mark.parametrize("first_error", [_status_error(429), _status_error(503)])
def test_retryable_soclaas_statuses_back_off_then_succeed(settings, first_error):
    sleeps = []
    client, sequence = _client_with_completions(settings, [first_error, '{"ok":true}'], sleeps)
    assert client._completion([{"role": "user", "content": "test"}]) == '{"ok":true}'
    assert sequence.calls == 2
    assert len(sleeps) == 1


def test_soclaas_timeout_backs_off_then_succeeds(settings):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    sleeps = []
    client, sequence = _client_with_completions(
        settings,
        [APITimeoutError(request=request), '{"ok":true}'],
        sleeps,
    )
    assert client._completion([{"role": "user", "content": "test"}]) == '{"ok":true}'
    assert sequence.calls == 2
    assert len(sleeps) == 1

