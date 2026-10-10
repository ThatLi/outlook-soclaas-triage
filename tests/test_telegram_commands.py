from outlook_triage.telegram_commands import BOT_COMMANDS, format_body_preview, parse_command


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
    assert parse_command("/list").action == "list"
    assert parse_command("/sync@triage_bot").action == "sync"
    assert parse_command("/status").action == "status"
    assert parse_command("/retry").limit == 50
    assert parse_command("/retry 100").limit == 100
    assert parse_command("/tasks").option == "all"
    assert parse_command("/tasks WAITING").option == "waiting"
    assert parse_command("/waiting #4 5 #4").task_ids == (4, 5)
    assert parse_command("/reopen 8").task_ids == (8,)
    assert {command for command, _ in BOT_COMMANDS} >= {"list", "sync", "retry", "tasks"}


def test_parse_rejects_malformed_commands():
    for value in (
        "read 1", "/read", "/read #0", "/read -1", "/help now", "/delete #1",
        "/show #1 #2", "/done", "/dismiss #0", "/list now", "/sync now",
        "/status now", "/retry 0", "/retry 101", "/retry many", "/tasks unknown",
    ):
        assert parse_command(value) is None


def test_body_preview_is_escaped_chunked_and_truncated():
    chunks = format_body_preview("A < B", "<secret> " * 1000, preview_chars=6000, max_plain_chars=500)
    assert len(chunks) > 1
    assert all("<secret>" not in chunk for chunk in chunks)
    assert "A &lt; B" in chunks[0]
    assert "Preview truncated." in chunks[-1]
    assert all(len(chunk.replace("&lt;", "<").replace("&gt;", ">")) < 700 for chunk in chunks)
