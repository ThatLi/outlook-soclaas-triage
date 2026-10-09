from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
import logging
from logging import Logger

import pytest
import requests

from outlook_triage import cli
from outlook_triage import telegram_delivery
from outlook_triage.database import Database
from outlook_triage.filters import FilterRules
from outlook_triage.logging_utils import configure_logging
from outlook_triage.models import EmailClassification
from outlook_triage.service import process_message
from outlook_triage.telegram import TelegramClient, TelegramError


FULL_ENTRY_ID = "00000000VERY-SENSITIVE-OUTLOOK-ENTRY-ID-123456789ABC"
BODY_SECRET = "private email body: acquisition code ORCHID-9281"
DIGEST_SECRET = "private digest: executive action BLUEBIRD-771"
API_KEY = "soclaas-key-should-never-appear"
BOT_TOKEN = "123456:telegram-token-should-never-appear"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def _message(key: str, source_id: str = FULL_ENTRY_ID) -> dict:
    return {
        "messageKey": key,
        "sourceId": source_id,
        "storeId": "store-secret",
        "conversationId": "conversation-secret",
        "sender": {"emailAddress": {"name": "Sensitive Sender", "address": "private@example.test"}},
        "subject": "Confidential subject BLUEBIRD",
        "receivedDateTime": "2026-10-08T08:00:00+08:00",
        "importance": "normal",
        "hasAttachments": False,
        "body": {"contentType": "text", "content": BODY_SECRET},
    }


class SuccessfulAI:
    def classify(self, email, now):
        assert BODY_SECRET in email.body
        return EmailClassification.model_validate({
            "requires_action": False,
            "urgency": "none",
            "action_type": "none",
            "task": None,
            "deadline": None,
            "deadline_raw": None,
            "summary": "Sensitive summary that remains in the database",
            "category": "information",
            "reason": "No action requested",
            "confidence": 0.95,
        })


class FailingAI:
    def classify(self, email, now):
        assert BODY_SECRET in email.body
        raise RuntimeError("sanitized model failure")


class OfflineSession:
    def post(self, url, data, timeout):
        assert url == TELEGRAM_URL
        assert DIGEST_SECRET in data["text"]
        raise requests.ConnectionError("offline")


def _close_logger(logger: Logger) -> None:
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def test_classification_logs_only_abbreviated_outlook_identifier(settings, capsys):
    logger = configure_logging(settings.log_file, verbose=True)
    db = Database(settings.database_file)
    configured = replace(settings, soclaas_api_key=API_KEY)
    first = _message("store-secret:first")
    second_id = "ANOTHER-PRIVATE-ENTRY-ID-ABCDEFGHIJKL"
    second = _message("store-secret:second", second_id)
    try:
        db.store_sync_batch([first, second], "2026-10-08T08:00:00+08:00")
        assert process_message(
            first, db=db, ai=SuccessfulAI(), rules=FilterRules(), settings=configured, logger=logger
        ) == "processed"
        assert process_message(
            second, db=db, ai=FailingAI(), rules=FilterRules(), settings=configured, logger=logger
        ) == "failed"
    finally:
        db.close()
        _close_logger(logger)

    file_log = settings.log_file.read_text(encoding="utf-8")
    console_log = capsys.readouterr().err
    combined = file_log + console_log
    assert FULL_ENTRY_ID[-12:] in file_log
    assert second_id[-12:] in file_log
    assert "sanitized model failure" in file_log
    for forbidden in (
        FULL_ENTRY_ID,
        second_id,
        BODY_SECRET,
        "Confidential subject BLUEBIRD",
        "private@example.test",
        API_KEY,
    ):
        assert forbidden not in combined


def test_sanitized_telegram_failure_excludes_credentials_payload_and_url(monkeypatch, settings):
    configured = replace(
        settings,
        telegram_bot_token=BOT_TOKEN,
        telegram_chat_id="private-chat-123",
        telegram_timeout_seconds=0.01,
    )
    client = TelegramClient(
        BOT_TOKEN,
        configured.telegram_chat_id,
        timeout=configured.telegram_timeout_seconds,
        session=OfflineSession(),
        sleep=lambda _: None,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "build_digest", lambda *args: f"# Digest\n\n- {DIGEST_SECRET}")
    monkeypatch.setattr(telegram_delivery, "TelegramClient", lambda *args, **kwargs: client)

    with pytest.raises(TelegramError, match="unreachable after retries") as captured:
        cli.run(Namespace(command="digest", telegram=True, verbose=True))

    logger = logging.getLogger("outlook_triage")
    for handler in list(logger.handlers):
        handler.flush()
    file_log = configured.log_file.read_text(encoding="utf-8")
    error_text = str(captured.value)
    assert "Telegram digest delivery failed" in file_log
    for forbidden in (BOT_TOKEN, configured.telegram_chat_id, DIGEST_SECRET, TELEGRAM_URL, "sendMessage"):
        assert forbidden not in file_log
        assert forbidden not in error_text
    report = next(configured.reports_dir.glob("*.md"))
    assert DIGEST_SECRET in report.read_text(encoding="utf-8")
    _close_logger(logger)

