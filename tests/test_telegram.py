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


def _outlook_digest(*, overdue=None, urgent=None, upcoming=None, other=None, waiting=None):
    sections = (
        ("Overdue", overdue or []),
        ("Urgent / due today", urgent or []),
        ("Due within 7 days", upcoming or []),
        ("Other open actions", other or []),
        ("Waiting / follow-up", waiting or []),
    )
    lines = [
        "# Inbox brief — 2026-10-09",
        "",
        "Last successful Outlook sync: 2026-10-09T07:58:00+08:00",
        "",
        "> **Warning:** Some messages are pending or failed classification. Run `outlook-triage retry-failed`.",
    ]
    for heading, tasks in sections:
        lines.extend(["", f"## {heading}"])
        lines.extend(tasks or ["- None"])
    lines.extend(
        [
            "",
            "## Last 24 hours",
            "- Informational: 12",
            "- Newsletters: 4",
            "- Automated: 2",
            "- Explicitly skipped: 0",
            "- Pending or failed: 2",
        ]
    )
    return "\n".join(lines)


def test_format_digest_escapes_and_formats():
    chunks = format_digest("# Digest <today>\n\n## Urgent\n\n- Reply to A & B `today`")
    assert chunks == ["<b>Digest &lt;today&gt;</b>\n\n<b>Urgent</b>\n\n• Reply to A &amp; B today"]


def test_format_digest_chunks_long_unicode_content():
    chunks = format_digest("# Digest\n\n- " + "邮件U0001f4e8 " * 2500, max_plain_chars=200)
    assert len(chunks) > 2
    assert all(len(chunk) < 1000 for chunk in chunks)


def test_empty_digest_has_safe_fallback():
    assert "No digest content" in format_digest("")[0]


def test_outlook_digest_uses_summary_and_priority_messages():
    markdown = _outlook_digest(
        overdue=["- [#42] Submit <expense> report — due 2026-10-08 (Finance & Ops)"],
        urgent=["- [#51] Confirm deployment window — due 2026-10-09 (Engineering)"],
        upcoming=["- [#56] Review Q4 forecast — due 2026-10-11 (Finance)"],
        other=["- [#68] Review architecture proposal (Engineering)"],
        waiting=["- [#33] Vendor security questionnaire (Acme)"],
    )

    chunks = format_digest(markdown)

    assert len(chunks) == 3
    summary, immediate, rare = chunks
    assert summary.startswith("📬 <b>INBOX BRIEF · 9 OCT 2026</b>")
    assert "<b>OPEN ACTIONS</b>\n\n🔴 Overdue: <b>1</b>" in summary
    assert "\n\n📊 <b>LAST 24 HOURS</b>\n\n• Informational: <b>12</b>" in summary
    assert "\n\n🔄 <b>SYNCHRONIZATION</b>\n\n✅ Last successful sync: <b>09 Oct, 07:58</b>" in summary
    assert "⚠️ Some messages are pending or failed classification." in summary

    assert "🔴 <b>OVERDUE · 1</b>\n\n☐ <code>#42</code> Submit &lt;expense&gt; report" in immediate
    assert "Due <b>8 Oct</b> · Finance &amp; Ops" in immediate
    assert "\n\n🟠 <b>URGENT / DUE TODAY · 1</b>\n\n" in immediate
    assert "Due <b>today</b> · Engineering" in immediate
    assert "\n\n🟡 <b>DUE WITHIN 7 DAYS · 1</b>\n\n" in immediate

    assert "DUE WITHIN" not in rare
    assert "⚪ <b>OTHER OPEN ACTIONS · 1</b>\n\n" in rare
    assert "\n\n💤 <b>WAITING / FOLLOW-UP · 1</b>\n\n" in rare


def test_outlook_digest_omits_empty_priority_sections():
    chunks = format_digest(_outlook_digest(urgent=["- [#1] Act now (Alice)"]))

    assert len(chunks) == 2
    assert "Overdue: <b>0</b>" in chunks[0]
    assert "Other open actions: <b>0</b>" in chunks[0]
    assert "OVERDUE ·" not in chunks[1]
    assert "OTHER OPEN ACTIONS ·" not in chunks[1]
    assert "URGENT / DUE TODAY · 1" in chunks[1]


def test_outlook_digest_splits_large_section_with_repeated_headings():
    tasks = [
        f"- [#{index}] Review item {index} with supporting context " + ("detail " * 12) + "(Operations)"
        for index in range(1, 81)
    ]
    chunks = format_digest(_outlook_digest(overdue=tasks))

    priority_chunks = chunks[1:]
    assert len(priority_chunks) > 1
    assert all("🔴 <b>OVERDUE · 80</b> — PART " in chunk for chunk in priority_chunks)
    assert all(len(chunk) < 4096 for chunk in priority_chunks)
    joined = "\n".join(priority_chunks)
    for index in range(1, 81):
        assert joined.count(f"<code>#{index}</code>") == 1


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
