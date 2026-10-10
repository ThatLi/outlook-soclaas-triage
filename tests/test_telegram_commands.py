from outlook_triage.telegram_commands import format_body_preview, parse_command


def test_parse_supported_commands_and_bot_suffixes():
    assert parse_command("/read #42").action == "read"
    assert parse_command("/read 42").task_id == 42
    assert parse_command("/read #42 7 #42 9").task_ids == (42, 7, 9)
    assert parse_command("/done #42 7 #42 9").task_ids == (42, 7, 9)
    assert parse_command("/done #42").action == "done"
    assert parse_command("/dismiss@triage_bot 7 #9").task_ids == (7, 9)
    assert parse_command("/dismiss #42").action == "dismiss"
    assert parse_command("/show@triage_bot #7").task_id == 7
    assert parse_command("/start").action == "help"
    assert parse_command("/help").action == "help"


def test_parse_rejects_malformed_commands():
    for value in (
        "read 1", "/read", "/read #0", "/read -1", "/help now", "/delete #1",
        "/show #1 #2", "/done", "/dismiss #0",
    ):
        assert parse_command(value) is None


def test_body_preview_is_escaped_chunked_and_truncated():
    chunks = format_body_preview("A < B", "<secret> " * 1000, preview_chars=6000, max_plain_chars=500)
    assert len(chunks) > 1
    assert all("<secret>" not in chunk for chunk in chunks)
    assert "A &lt; B" in chunks[0]
    assert "Preview truncated." in chunks[-1]
    assert all(len(chunk.replace("&lt;", "<").replace("&gt;", ">")) < 700 for chunk in chunks)
