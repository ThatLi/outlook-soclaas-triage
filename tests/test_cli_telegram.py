from __future__ import annotations

from argparse import Namespace
from dataclasses import replace

import pytest

from outlook_triage import cli
from outlook_triage.telegram import TelegramError


def test_digest_telegram_flag_is_opt_in():
    parser = cli.build_parser()
    assert parser.parse_args(["digest"]).telegram is False
    assert parser.parse_args(["digest", "--telegram"]).telegram is True


def test_local_digest_does_not_construct_telegram(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_digest", lambda *args: "# Local only")
    monkeypatch.setattr(cli, "TelegramClient", lambda *args, **kwargs: pytest.fail("Telegram was invoked"))
    result = cli.run(Namespace(command="digest", telegram=False, verbose=False))
    assert result == 0
    assert list(settings.reports_dir.glob("*.md"))[0].read_text(encoding="utf-8") == "# Local only"


def test_telegram_failure_preserves_saved_digest(monkeypatch, settings):
    configured = replace(settings, telegram_bot_token="secret", telegram_chat_id="7")
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "build_digest", lambda *args: "# Preserved")

    class FailingTelegram:
        def __init__(self, *args, **kwargs):
            pass

        def send_digest(self, text):
            raise TelegramError("sanitized failure")

    monkeypatch.setattr(cli, "TelegramClient", FailingTelegram)
    with pytest.raises(TelegramError, match="sanitized failure"):
        cli.run(Namespace(command="digest", telegram=True, verbose=False))
    reports = list(configured.reports_dir.glob("*.md"))
    assert reports and reports[0].read_text(encoding="utf-8") == "# Preserved"


def test_scheduler_supports_opt_in_telegram_action():
    script = (cli.Path(__file__).parents[1] / "scripts" / "register_windows_tasks.ps1").read_text(encoding="utf-8")
    assert "[switch]$EnableTelegram" in script
    assert '"digest --telegram"' in script
