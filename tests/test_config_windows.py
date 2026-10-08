from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from outlook_triage import cli
from outlook_triage.config import ConfigurationError, load_settings


CONFIG_ENV_NAMES = (
    "OUTLOOK_TRIAGE_CONFIG_DIR",
    "OUTLOOK_TRIAGE_DATA_DIR",
    "OUTLOOK_TRIAGE_STATE_DIR",
    "OUTLOOK_TRIAGE_SECRETS_FILE",
    "OUTLOOK_TRIAGE_RULES_FILE",
    "OUTLOOK_TRIAGE_DATABASE",
    "OUTLOOK_TRIAGE_REPORTS_DIR",
    "OUTLOOK_TRIAGE_LOG_FILE",
    "OUTLOOK_TRIAGE_LOCK_DIR",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_TIMEOUT_SECONDS",
)


def _isolated_windows_environment(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return roaming, local


def test_windows_default_paths_use_appdata_and_localappdata(monkeypatch, tmp_path):
    roaming, local = _isolated_windows_environment(monkeypatch, tmp_path)

    settings = load_settings()

    assert settings.config_dir == roaming / "OutlookTriage"
    assert settings.secrets_file == roaming / "OutlookTriage" / "secrets.env"
    assert settings.data_dir == local / "OutlookTriage"
    assert settings.state_dir == local / "OutlookTriage" / "state"
    assert settings.database_file == local / "OutlookTriage" / "outlook-triage.db"
    assert settings.reports_dir == local / "OutlookTriage" / "reports"
    assert settings.log_file == local / "OutlookTriage" / "logs" / "outlook-triage.log"
    assert settings.lock_dir == local / "OutlookTriage" / "state" / "sync.lock"


def test_telegram_values_load_from_secrets_file(monkeypatch, tmp_path):
    roaming, _ = _isolated_windows_environment(monkeypatch, tmp_path)
    secrets = roaming / "OutlookTriage" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text(
        "TELEGRAM_BOT_TOKEN=file-token\n"
        "TELEGRAM_CHAT_ID=123456\n"
        "TELEGRAM_TIMEOUT_SECONDS=12.5\n",
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.telegram_bot_token == "file-token"
    assert settings.telegram_chat_id == "123456"
    assert settings.telegram_timeout_seconds == 12.5


def test_environment_overrides_telegram_secrets_file(monkeypatch, tmp_path):
    roaming, _ = _isolated_windows_environment(monkeypatch, tmp_path)
    secrets = roaming / "OutlookTriage" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text(
        "TELEGRAM_BOT_TOKEN=file-token\n"
        "TELEGRAM_CHAT_ID=file-chat\n"
        "TELEGRAM_TIMEOUT_SECONDS=12\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "environment-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "environment-chat")
    monkeypatch.setenv("TELEGRAM_TIMEOUT_SECONDS", "4.25")

    settings = load_settings()

    assert settings.telegram_bot_token == "environment-token"
    assert settings.telegram_chat_id == "environment-chat"
    assert settings.telegram_timeout_seconds == 4.25


def test_whitespace_only_telegram_values_are_missing(monkeypatch, tmp_path):
    _isolated_windows_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "\t")
    monkeypatch.setenv("TELEGRAM_TIMEOUT_SECONDS", "   ")

    settings = load_settings()

    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert settings.telegram_timeout_seconds == 20.0
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"):
        settings.require_telegram()


def test_invalid_telegram_timeout_is_rejected(monkeypatch, tmp_path):
    _isolated_windows_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ValueError, match="could not convert string to float"):
        load_settings()


def test_telegram_chats_requires_token_but_not_chat_id(monkeypatch, settings, capsys):
    configured = replace(settings, telegram_bot_token="token", telegram_chat_id=None)
    captured = {}

    class FakeTelegram:
        def __init__(self, token, chat_id=None, *, timeout):
            captured.update(token=token, chat_id=chat_id, timeout=timeout)

        def recent_private_chats(self):
            return [type("Chat", (), {"chat_id": 42, "display_name": "Private user"})()]

    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "TelegramClient", FakeTelegram)

    assert cli.run(Namespace(command="telegram-chats", verbose=False)) == 0
    assert captured == {"token": "token", "chat_id": None, "timeout": 20.0}
    assert "42\tPrivate user" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["telegram-test", "digest"])
def test_telegram_delivery_commands_require_token_and_chat_id(monkeypatch, settings, command):
    unconfigured = replace(settings, telegram_bot_token="token", telegram_chat_id=None)
    monkeypatch.setattr(cli, "load_settings", lambda: unconfigured)
    monkeypatch.setattr(cli, "build_digest", lambda *args: "# Saved before validation")
    monkeypatch.setattr(cli, "TelegramClient", lambda *args, **kwargs: pytest.fail("client was constructed"))
    args = Namespace(command=command, verbose=False, telegram=True) if command == "digest" else Namespace(command=command, verbose=False)

    with pytest.raises(ConfigurationError, match="TELEGRAM_CHAT_ID"):
        cli.run(args)

    if command == "digest":
        reports = list(unconfigured.reports_dir.glob("*.md"))
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8") == "# Saved before validation"
