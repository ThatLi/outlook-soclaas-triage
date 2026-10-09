from __future__ import annotations

from argparse import Namespace
from dataclasses import replace

import pytest

from outlook_triage import cli
from outlook_triage.telegram import TelegramError
from outlook_triage.telegram_delivery import DeliveryResult


def test_digest_telegram_flag_is_opt_in():
    parser = cli.build_parser()
    assert parser.parse_args(["digest"]).telegram is False
    assert parser.parse_args(["digest", "--telegram"]).telegram is True
    assert parser.parse_args(["telegram-retry"]).command == "telegram-retry"


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

    def fail_delivery(*args, **kwargs):
        raise TelegramError("sanitized failure", category="transient")

    monkeypatch.setattr(cli, "deliver_pending", fail_delivery)
    with pytest.raises(TelegramError, match="sanitized failure"):
        cli.run(Namespace(command="digest", telegram=True, verbose=False))
    reports = list(configured.reports_dir.glob("*.md"))
    assert reports and reports[0].read_text(encoding="utf-8") == "# Preserved"


def test_digest_retry_reuses_frozen_same_day_delivery(monkeypatch, settings):
    configured = replace(settings, telegram_bot_token="secret", telegram_chat_id="7")
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "build_digest", lambda *args: "# Frozen")

    attempts = []

    def first_attempt(*args, **kwargs):
        attempts.append("failed")
        raise TelegramError("offline", category="transient")

    monkeypatch.setattr(cli, "deliver_pending", first_attempt)
    with pytest.raises(TelegramError, match="offline"):
        cli.run(Namespace(command="digest", telegram=True, verbose=False))

    monkeypatch.setattr(cli, "build_digest", lambda *args: pytest.fail("digest was regenerated"))
    monkeypatch.setattr(cli, "deliver_pending", lambda *args, **kwargs: DeliveryResult(completed_deliveries=1))

    assert cli.run(Namespace(command="digest", telegram=True, verbose=False)) == 0
    assert attempts == ["failed"]


def test_permanent_delivery_failure_is_handled_without_scheduler_retry(monkeypatch, settings):
    configured = replace(settings, telegram_bot_token="bad", telegram_chat_id="7")
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "build_digest", lambda *args: "# Queued")
    monkeypatch.setattr(
        cli,
        "deliver_pending",
        lambda *args, **kwargs: DeliveryResult(needs_attention=1),
    )

    assert cli.run(Namespace(command="digest", telegram=True, verbose=False)) == 0


def test_scheduler_supports_opt_in_telegram_action():
    script = (cli.Path(__file__).parents[1] / "scripts" / "register_windows_tasks.ps1").read_text(encoding="utf-8")
    assert "[switch]$EnableTelegram" in script
    assert '"digest --telegram"' in script


def test_scheduler_resolves_default_project_dir_inside_script_body():
    script = (cli.Path(__file__).parents[1] / "scripts" / "register_windows_tasks.ps1").read_text(encoding="utf-8")
    assert "[string]$ProjectDir =" not in script
    assert "$scriptPath = $PSCommandPath" in script
    assert "$scriptPath = $MyInvocation.MyCommand.Path" in script
