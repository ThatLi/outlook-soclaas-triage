from __future__ import annotations

import html
import logging
import os
import time
from typing import Any, Callable

from .config import Settings
from .database import Database
from .locking import AlreadyRunning, process_lock
from .outlook import OutlookClient, OutlookError
from .telegram import TelegramClient, TelegramError
from .telegram_commands import HELP_TEXT, format_body_preview, parse_command


TERMINAL_UPDATE_STATES = {"responded", "failed"}


def _mark_read(
    rows: list[Any],
    update_id: int,
    db: Database,
    settings: Settings,
    *,
    outlook_factory: Callable[..., OutlookClient],
) -> None:
    with process_lock(settings.lock_dir):
        with outlook_factory(settings.outlook_profile, timezone=settings.timezone) as outlook:
            for row in rows:
                outlook.mark_read(str(row["source_id"]), str(row["store_id"]))
                db.mark_telegram_update_task_applied(update_id, int(row["task_id"]))


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
    task_ids = command.task_ids if command else ()
    db.record_telegram_update(update_id, str(settings.telegram_chat_id), command_name, task_ids)

    if command is None:
        telegram.send_plain_message(f"Command not recognized.\n\n{HELP_TEXT}")
        db.set_telegram_update_status(update_id, "responded")
        return True
    if command.action == "help":
        telegram.send_plain_message(HELP_TEXT)
        db.set_telegram_update_status(update_id, "responded")
        return True

    rows_by_id = {
        task_id: row
        for task_id in command.task_ids
        if (row := db.task_email(task_id)) is not None
    }
    missing = [task_id for task_id in command.task_ids if task_id not in rows_by_id]
    if missing:
        missing_labels = ", ".join(f"<code>#{task_id}</code>" for task_id in missing)
        noun = "Task" if len(missing) == 1 else "Tasks"
        verb = "was" if len(missing) == 1 else "were"
        telegram.send_message(f"{noun} {missing_labels} {verb} not found. No emails were changed.")
        db.set_telegram_update_status(update_id, "failed", error_category="not_found")
        return True

    try:
        if command.action == "read":
            pending_ids = db.telegram_update_task_ids(update_id, status="pending")
            if pending_ids:
                _mark_read(
                    [rows_by_id[task_id] for task_id in pending_ids],
                    update_id,
                    db,
                    settings,
                    outlook_factory=outlook_factory,
                )
                db.set_telegram_update_status(update_id, "applied")
            if len(command.task_ids) == 1:
                task_id = command.task_ids[0]
                telegram.send_message(
                    f"✅ Email is marked as read: <code>#{task_id}</code> — "
                    f"{html.escape(str(rows_by_id[task_id]['subject']))}"
                )
            else:
                labels = ", ".join(f"<code>#{task_id}</code>" for task_id in command.task_ids)
                telegram.send_message(
                    f"✅ {len(command.task_ids)} emails are marked as read: {labels}"
                )
        else:
            _show_body(
                rows_by_id[command.task_ids[0]], telegram, settings,
                outlook_factory=outlook_factory,
            )
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


def ensure_polling_available(telegram: TelegramClient) -> None:
    if str(telegram.webhook_info().get("url") or "").strip():
        raise TelegramError(
            "Telegram polling is unavailable while a webhook is configured; clear the webhook before starting the listener",
            category="configuration",
        )


def poll_once(
    db: Database,
    settings: Settings,
    telegram: TelegramClient,
    logger: logging.Logger,
    *,
    timeout: int = 30,
    outlook_factory: Callable[..., OutlookClient] = OutlookClient,
) -> int:
    updates = telegram.get_updates(offset=db.telegram_offset(), timeout=timeout)
    return process_updates(
        db, settings, telegram, updates, logger, outlook_factory=outlook_factory
    )


def listen(
    db: Database,
    settings: Settings,
    telegram: TelegramClient,
    logger: logging.Logger,
    *,
    timeout: int = 30,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    outlook_factory: Callable[..., OutlookClient] = OutlookClient,
) -> int:
    processed = 0
    failures = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        receiver_lock = settings.state_dir / "telegram-receiver.lock"
        if receiver_lock.exists():
            os.utime(receiver_lock, None)
        try:
            processed += poll_once(
                db, settings, telegram, logger, timeout=timeout, outlook_factory=outlook_factory
            )
            failures = 0
        except TelegramError as exc:
            if exc.category != "transient":
                raise
            failures += 1
            delay = min(60.0, float(2 ** min(failures - 1, 6)))
            logger.warning("Telegram listener temporarily unavailable; retrying in %.0f seconds", delay)
            sleep(delay)
    return processed
