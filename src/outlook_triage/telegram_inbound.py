from __future__ import annotations

import html
import logging
from typing import Any, Callable

from .config import Settings
from .database import Database
from .locking import AlreadyRunning, process_lock
from .outlook import OutlookClient, OutlookError
from .telegram import TelegramClient, TelegramError
from .telegram_commands import HELP_TEXT, TelegramCommand, format_body_preview, parse_command


TERMINAL_UPDATE_STATES = {"responded", "failed"}


def _mark_read(
    row: Any,
    settings: Settings,
    *,
    outlook_factory: Callable[..., OutlookClient],
) -> None:
    with process_lock(settings.lock_dir):
        with outlook_factory(settings.outlook_profile, timezone=settings.timezone) as outlook:
            outlook.mark_read(str(row["source_id"]), str(row["store_id"]))


def _show_body(
    row: Any,
    telegram: TelegramClient,
    settings: Settings,
    *,
    outlook_factory: Callable[..., OutlookClient],
) -> None:
    with process_lock(settings.lock_dir):
        with outlook_factory(settings.outlook_profile, timezone=settings.timezone) as outlook:
            body = outlook.get_body_preview(
                str(row["source_id"]), str(row["store_id"]),
                limit=settings.telegram_body_preview_chars + 1,
            )
    for chunk in format_body_preview(
        str(row["subject"]), body, preview_chars=settings.telegram_body_preview_chars
    ):
        telegram.send_message(chunk)


def process_update(
    db: Database,
    settings: Settings,
    telegram: TelegramClient,
    update: dict[str, Any],
    logger: logging.Logger,
    *,
    outlook_factory: Callable[..., OutlookClient] = OutlookClient,
) -> bool:
    """Process one update. Return True when its offset may be advanced."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return True
    message = update.get("message")
    if not isinstance(message, dict):
        return True
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or str(chat.get("id", "")) != str(settings.telegram_chat_id):
        return True
    if isinstance(sender, dict) and sender.get("is_bot"):
        return True

    existing = db.telegram_update(update_id)
    if existing is not None and str(existing["status"]) in TERMINAL_UPDATE_STATES:
        return True

    command = parse_command(str(message.get("text") or ""))
    command_name = command.action if command else "invalid"
    task_id = command.task_id if command else None
    db.record_telegram_update(update_id, str(settings.telegram_chat_id), command_name, task_id)

    if command is None:
        telegram.send_plain_message(f"Command not recognized.\n\n{HELP_TEXT}")
        db.set_telegram_update_status(update_id, "responded")
        return True
    if command.action == "help":
        telegram.send_plain_message(HELP_TEXT)
        db.set_telegram_update_status(update_id, "responded")
        return True

    row = db.task_email(command.task_id or 0)
    if row is None:
        telegram.send_message(f"Task <code>#{command.task_id}</code> was not found.")
        db.set_telegram_update_status(update_id, "failed", error_category="not_found")
        return True

    try:
        current = db.telegram_update(update_id)
        if command.action == "read":
            if current is None or str(current["status"]) != "applied":
                _mark_read(row, settings, outlook_factory=outlook_factory)
                db.set_telegram_update_status(update_id, "applied")
            telegram.send_message(
                f"✅ Email is marked as read: <code>#{command.task_id}</code> — "
                f"{html.escape(str(row['subject']))}"
            )
        else:
            _show_body(row, telegram, settings, outlook_factory=outlook_factory)
        db.set_telegram_update_status(update_id, "responded")
        return True
    except AlreadyRunning:
        telegram.send_plain_message("Outlook is busy with synchronization. Please try the command again shortly.")
        db.set_telegram_update_status(update_id, "failed", error_category="busy")
        return True
    except OutlookError as exc:
        logger.warning("Telegram Outlook command failed: %s", exc)
        telegram.send_plain_message("The Outlook operation failed. Check the local application log and try again.")
        db.set_telegram_update_status(update_id, "failed", error_category="outlook")
        return True


def process_updates(
    db: Database,
    settings: Settings,
    telegram: TelegramClient,
    updates: list[dict[str, Any]],
    logger: logging.Logger,
    *,
    outlook_factory: Callable[..., OutlookClient] = OutlookClient,
) -> int:
    processed = 0
    for update in sorted(updates, key=lambda item: int(item.get("update_id", -1))):
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        if not process_update(
            db, settings, telegram, update, logger, outlook_factory=outlook_factory
        ):
            break
        db.set_telegram_offset(update_id + 1)
        processed += 1
    return processed
