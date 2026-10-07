from __future__ import annotations

import requests
import pytest

from outlook_triage.telegram import TelegramClient, TelegramError, format_digest


class Response:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self.payload = payload if payload is not None else {"ok": True, "result": {}}
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("bad json")
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data, timeout):
        self.calls.append((url, data, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_format_digest_escapes_and_formats():
    chunks = format_digest("# Digest <today>\n\n## Urgent\n\n- Reply to A & B `today`")
    assert chunks == ["<b>Digest &lt;today&gt;</b>\n\n<b>Urgent</b>\n\n• Reply to A &amp; B today"]


def test_format_digest_chunks_long_unicode_content():
    chunks = format_digest("# Digest\n\n- " + "邮件U0001f4e8 " * 2500, max_plain_chars=200)
    assert len(chunks) > 2
    assert all(len(chunk) < 1000 for chunk in chunks)


def test_empty_digest_has_safe_fallback():
    assert "No digest content" in format_digest("")[0]


def test_recent_private_chats_deduplicates_and_ignores_groups():
    session = Session([Response(payload={"ok": True, "result": [
        {"message": {"chat": {"id": 7, "type": "private", "first_name": "A"}}},
        {"message": {"chat": {"id": 7, "type": "private", "first_name": "A"}}},
        {"message": {"chat": {"id": -2, "type": "group", "title": "No"}}},
    ]})])
    chats = TelegramClient("secret", session=session).recent_private_chats()
    assert [(chat.chat_id, chat.display_name) for chat in chats] == [("7", "A")]


def test_send_digest_delivers_chunks_in_order():
    chunks = format_digest("# One\n\n" + ("x" * 3500))
    session = Session([Response() for _ in chunks])
    client = TelegramClient("secret", "7", session=session)
    count = client.send_digest("# One\n\n" + ("x" * 3500))
    assert count == len(chunks)
    assert [call[1]["text"] for call in session.calls] == chunks
    assert all(call[1]["chat_id"] == "7" for call in session.calls)


def test_partial_delivery_reports_chunk_without_token():
    total = len(format_digest("# One\n\n" + ("x" * 3500)))
    session = Session([Response(), Response(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})])
    with pytest.raises(TelegramError) as captured:
        TelegramClient("super-secret-token", "7", session=session).send_digest("# One\n\n" + ("x" * 3500))
    assert f"message 2 of {total}" in str(captured.value)
    assert "super-secret-token" not in str(captured.value)


def test_429_respects_retry_after():
    delays = []
    session = Session([Response(429, {"ok": False, "parameters": {"retry_after": 3}}), Response()])
    TelegramClient("secret", "7", session=session, sleep=delays.append).send_message("hello")
    assert delays == [3.0]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_non_retryable_errors(status):
    session = Session([Response(status, {"ok": False, "description": "Unauthorized token value"})])
    with pytest.raises(TelegramError) as captured:
        TelegramClient("secret-value", "7", session=session).send_message("hello")
    assert "secret-value" not in str(captured.value)
    assert len(session.calls) == 1


def test_network_and_5xx_are_retried():
    session = Session([requests.ConnectionError("offline"), Response(500, {"ok": False}), Response()])
    delays = []
    TelegramClient("secret", "7", session=session, sleep=delays.append).send_message("hello")
    assert len(session.calls) == 3
    assert len(delays) == 2


def test_invalid_json_is_sanitized():
    session = Session([Response(200, invalid_json=True)])
    with pytest.raises(TelegramError, match="invalid response"):
        TelegramClient("secret", "7", session=session).send_message("hello")
