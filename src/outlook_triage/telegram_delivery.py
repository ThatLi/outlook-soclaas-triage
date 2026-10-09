from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .database import Database
from .telegram import TelegramClient, TelegramError, format_digest


DELIVERY_RETENTION_DAYS = 7
PERMANENT_CATEGORIES = {"configuration", "credentials", "access", "chat", "content", "permanent"}
GLOBAL_FAILURE_CATEGORIES = {"configuration", "credentials", "access", "chat"}


@dataclass(frozen=True)
class DeliveryResult:
    completed_deliveries: int = 0
    delivered_messages: int = 0
    needs_attention: int = 0
    expired: int = 0


def enqueue_digest(db: Database, digest_date: str, markdown: str, created_at: str):
    return db.enqueue_telegram_delivery(digest_date, format_digest(markdown), created_at)


def launch_tray_notification(logger: logging.Logger) -> bool:
    if os.name != "nt":
        return False
    powershell = shutil.which("powershell.exe")
    script = Path(__file__).parents[2] / "scripts" / "show_telegram_notification.ps1"
    if not powershell or not script.is_file():
        logger.warning("Telegram needs attention; Windows tray notification is unavailable")
        return False
    try:
        subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script),
                "-Title",
                "Outlook Triage: Telegram needs attention",
                "-Message",
                "Check the local log, correct the Telegram configuration, then run outlook-triage telegram-retry.",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
    except OSError:
        logger.warning("Telegram needs attention; Windows tray notification could not be started")
        return False
    return True


def _delayed_notice(row) -> str:
    created = str(row["created_at"])
    try:
        display = datetime.fromisoformat(created).strftime("%d %b %Y, %H:%M")
    except ValueError:
        display = str(row["digest_date"])
    return (
        "⚠️ <b>Delayed delivery</b>\n\n"
        f"The inbox digest generated on <b>{display}</b> could not be delivered earlier. "
        "The messages below are the delayed digest."
    )


def _plain_fallback_notice() -> str:
    return (
        "Outlook Triage could not display a queued inbox digest because Telegram rejected its formatting. "
        "The digest remains saved locally. Check the local log, then run: outlook-triage telegram-retry"
    )


def _notify_once(db: Database, rows: list, now_text: str, logger: logging.Logger) -> None:
    unnotified = [int(row["id"]) for row in rows if not row["notified_at"]]
    if not unnotified:
        return
    if launch_tray_notification(logger):
        db.mark_telegram_notification_sent(unnotified, now_text)


def _mark_global_attention(db: Database, rows: list, error: TelegramError, now_text: str) -> None:
    for row in rows:
        db.mark_telegram_delivery_error(
            int(row["id"]),
            status="needs_attention",
            category=error.category,
            message=str(error),
            attempted_at=now_text,
        )


def deliver_pending(
    db: Database,
    settings: Settings,
    logger: logging.Logger,
    *,
    now: datetime,
) -> DeliveryResult:
    now_text = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(days=DELIVERY_RETENTION_DAYS)).isoformat(timespec="seconds")
    expired = db.expire_telegram_deliveries(cutoff)
    rows = db.pending_telegram_deliveries()
    if not rows:
        return DeliveryResult(expired=expired)
    rows = [db.start_telegram_attempt(int(row["id"]), now_text) for row in rows]

    try:
        telegram = TelegramClient(
            settings.telegram_bot_token or "",
            settings.telegram_chat_id,
            timeout=settings.telegram_timeout_seconds,
        )
    except TelegramError as exc:
        _mark_global_attention(db, rows, exc, now_text)
        _notify_once(db, rows, now_text, logger)
        logger.error("Telegram delivery requires user intervention: %s", exc)
        return DeliveryResult(needs_attention=len(rows), expired=expired)

    completed = 0
    delivered_messages = 0
    needs_attention = 0
    tray_started = False
    for row_index, row in enumerate(rows):
        delivery_id = int(row["id"])
        chunks = db.telegram_delivery_chunks(row)
        try:
            if int(row["attempt_count"]) > 1 and not row["delayed_notice_sent"]:
                telegram.send_message(_delayed_notice(row))
                db.mark_telegram_delayed_notice_sent(delivery_id)
                delivered_messages += 1

            start = int(row["next_chunk"])
            if start >= len(chunks):
                db.mark_telegram_chunk_sent(delivery_id, len(chunks), len(chunks), now_text)
            for index in range(start, len(chunks)):
                telegram.send_message(chunks[index])
                db.mark_telegram_chunk_sent(delivery_id, index + 1, len(chunks), now_text)
                delivered_messages += 1
            completed += 1
        except TelegramError as exc:
            logger.error("Telegram queued delivery %s failed: %s", delivery_id, exc)
            if exc.category == "transient":
                db.mark_telegram_delivery_error(
                    delivery_id,
                    status="pending",
                    category=exc.category,
                    message=str(exc),
                    attempted_at=now_text,
                )
                raise

            if exc.category == "content" and not row["fallback_notice_attempted"]:
                db.mark_telegram_fallback_attempted(delivery_id)
                try:
                    telegram.send_plain_message(_plain_fallback_notice())
                except TelegramError as fallback_error:
                    logger.error("Telegram plain-text fallback failed: %s", fallback_error)

            if exc.category in GLOBAL_FAILURE_CATEGORIES:
                affected = [row, *rows[row_index + 1 :]]
                _mark_global_attention(db, affected, exc, now_text)
                if not tray_started:
                    _notify_once(db, affected, now_text, logger)
                    tray_started = True
                needs_attention += len(affected)
                break

            db.mark_telegram_delivery_error(
                delivery_id,
                status="needs_attention",
                category=exc.category if exc.category in PERMANENT_CATEGORIES else "permanent",
                message=str(exc),
                attempted_at=now_text,
            )
            refreshed = db.active_telegram_delivery(str(row["digest_date"]))
            if refreshed is not None:
                if not tray_started:
                    _notify_once(db, [refreshed], now_text, logger)
                    tray_started = True
            needs_attention += 1

    return DeliveryResult(
        completed_deliveries=completed,
        delivered_messages=delivered_messages,
        needs_attention=needs_attention,
        expired=expired,
    )

