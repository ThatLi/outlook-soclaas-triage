from __future__ import annotations

import html
import logging
import os
import sqlite3
import time
from typing import Any, Callable

from .config import ConfigurationError, Settings
from .database import Database
from .digest import build_digest
from .locking import AlreadyRunning, process_lock
from .operations import run_retry, run_sync
from .outlook import OutlookClient, OutlookError
from .telegram import TelegramClient, TelegramError, format_digest
from .telegram_commands import HELP_TEXT, format_body_preview, format_status, format_task_list, parse_command


TERMINAL_UPDATE_STATES = {"responded", "failed"}


def _counts_message(action: str, counts: dict[str, int]) -> str:
    label = "Synchronization" if action in {"sync", "list"} else "Classification retry"
    message = (
        f"{label} complete: processed={counts.get('processed', 0)}, "
        f"skipped={counts.get('skipped', 0)}, failed={counts.get('failed', 0)}, "
        f"unchanged={counts.get('unchanged', 0)}."
    )
    if counts.get("failed", 0):
        message += " Some messages remain pending or failed; the digest may be incomplete."
    return message


def _send_live_digest(db: Database, settings: Settings, telegram: TelegramClient) -> None:
    markdown = build_digest(db, settings)
    for chunk in format_digest(markdown):
        telegram.send_message(chunk)


def _process_operation(
    command: Any,
    existing: Any,
    update_id: int,
    db: Database,
    settings: Settings,
    telegram: TelegramClient,
    logger: logging.Logger,
) -> bool:
    already_applied = existing is not None and str(existing["status"]) == "applied"
    try:
        if already_applied:
            if command.action == "list":
                telegram.send_plain_message(
                    "The synchronization already completed; here is the current live digest."
                )
                _send_live_digest(db, settings, telegram)
            else:
                telegram.send_plain_message(
                    "This operation already completed. Use /status for the current local state."
                )
            db.set_telegram_update_status(update_id, "responded")
            return True

        progress = {
            "list": "Synchronizing Outlook and building a fresh digest…",
            "sync": "Synchronizing Outlook…",
            "retry": f"Retrying up to {command.limit} pending or failed classifications…",
        }[command.action]
        telegram.send_plain_message(f"⏳ {progress}")
        counts = (
            run_retry(db, settings, logger, limit=command.limit)
            if command.action == "retry"
            else run_sync(db, settings, logger)
        )
        db.set_telegram_update_status(update_id, "applied")
        telegram.send_plain_message(_counts_message(command.action, counts))
        if command.action == "list":
            _send_live_digest(db, settings, telegram)
        db.set_telegram_update_status(update_id, "responded")
        return True
    except TelegramError:
        raise
    except AlreadyRunning:
        message, category = (
            "Outlook is busy with another operation. Please try again shortly.", "busy"
        )
    except ConfigurationError:
        message, category = (
            "Local configuration is incomplete. Check the application log and configuration, then try again.",
            "configuration",
        )
    except OutlookError:
        message, category = (
            "Outlook could not complete the operation. Check the local application log and try again.",
            "outlook",
        )
    except sqlite3.Error:
        message, category = (
            "The local database could not complete the operation. Check the local application log and try again.",
            "database",
        )
    except RuntimeError:
        message, category = (
            "The classification service could not complete the operation. Check the local application log and try again.",
            "classification",
        )
    except Exception:
        message, category = (
            "The operation could not be completed. Check the local application log and try again.",
            "operation",
        )
    logger.error("Telegram %s operation failed (%s)", command.action, category)
    telegram.send_plain_message(message)
    db.set_telegram_update_status(update_id, "failed", error_category=category)
    return True


def _apply_task_actions(
    rows: list[Any],
    update_id: int,
    db: Database,
    settings: Settings,
    *,
    task_status: str | None,
    outlook_factory: Callable[..., OutlookClient],
) -> None:
    with process_lock(settings.lock_dir):
        with outlook_factory(settings.outlook_profile, timezone=settings.timezone) as outlook:
            for row in rows:
                outlook.mark_read(str(row["source_id"]), str(row["store_id"]))
                db.apply_telegram_task(
                    update_id, int(row["task_id"]), task_status=task_status
                )


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
    if command.action in {"list", "sync", "retry"}:
        return _process_operation(
            command, existing, update_id, db, settings, telegram, logger
        )
    if command.action == "status":
        try:
            telegram.send_message(format_status(db.operational_status()))
            db.set_telegram_update_status(update_id, "responded")
            return True
        except sqlite3.Error as exc:
            logger.error("Telegram status query failed (%s)", type(exc).__name__)
            telegram.send_plain_message("The local status database could not be read. Check the local log and try again.")
            db.set_telegram_update_status(update_id, "failed", error_category="database")
            return True
    if command.action == "tasks":
        try:
            selected_status = None if command.option == "all" else command.option
            for chunk in format_task_list(db.list_tasks(selected_status), command.option or "all"):
                telegram.send_message(chunk)
            db.set_telegram_update_status(update_id, "responded")
            return True
        except sqlite3.Error as exc:
            logger.error("Telegram task-list query failed (%s)", type(exc).__name__)
            telegram.send_plain_message("The local task database could not be read. Check the local log and try again.")
            db.set_telegram_update_status(update_id, "failed", error_category="database")
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
        if command.action in {"read", "done", "dismiss", "waiting", "reopen"}:
            pending_ids = db.telegram_update_task_ids(update_id, status="pending")
            if pending_ids:
                task_status = {
                    "done": "done",
                    "dismiss": "dismissed",
                    "waiting": "waiting",
                    "reopen": "open",
                }.get(command.action)
                if command.action in {"waiting", "reopen"}:
                    for task_id in pending_ids:
                        db.apply_telegram_task(update_id, task_id, task_status=task_status)
                else:
                    _apply_task_actions(
                        [rows_by_id[task_id] for task_id in pending_ids],
                        update_id,
                        db,
                        settings,
                        task_status=task_status,
                        outlook_factory=outlook_factory,
                    )
                db.set_telegram_update_status(update_id, "applied")
            if command.action == "read" and len(command.task_ids) == 1:
                task_id = command.task_ids[0]
                telegram.send_message(
                    f"✅ Email is marked as read: <code>#{task_id}</code> — "
                    f"{html.escape(str(rows_by_id[task_id]['subject']))}"
                )
            elif command.action == "read":
                labels = ", ".join(f"<code>#{task_id}</code>" for task_id in command.task_ids)
                telegram.send_message(
                    f"✅ {len(command.task_ids)} emails are marked as read: {labels}"
                )
            elif command.action in {"waiting", "reopen"}:
                target = "waiting" if command.action == "waiting" else "open"
                labels = ", ".join(f"<code>#{task_id}</code>" for task_id in command.task_ids)
                noun = "Task" if len(command.task_ids) == 1 else f"{len(command.task_ids)} tasks"
                telegram.send_message(f"✅ {noun} marked {target}: {labels}")
            elif len(command.task_ids) == 1:
                task_id = command.task_ids[0]
                target = "done" if command.action == "done" else "dismissed"
                telegram.send_message(
                    f"✅ Task <code>#{task_id}</code> marked {target} and its email marked read — "
                    f"{html.escape(str(rows_by_id[task_id]['subject']))}"
                )
            else:
                target = "done" if command.action == "done" else "dismissed"
                labels = ", ".join(f"<code>#{task_id}</code>" for task_id in command.task_ids)
                telegram.send_message(
                    f"✅ {len(command.task_ids)} tasks marked {target} and their emails marked read: {labels}"
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
        applied = len(db.telegram_update_task_ids(update_id, status="applied"))
        if command.action in {"done", "dismiss"} and applied:
            telegram.send_plain_message(
                f"The Outlook operation failed after {applied} of {len(command.task_ids)} tasks completed. "
                "Completed changes were kept; remaining task statuses were not changed. "
                "Resending the command is safe."
            )
        else:
            telegram.send_plain_message(
                "The Outlook operation failed. Check the local application log and try again."
            )
        db.set_telegram_update_status(update_id, "failed", error_category="outlook")
        return True
    except (sqlite3.Error, KeyError) as exc:
        logger.error("Telegram task-state update failed (%s)", type(exc).__name__)
        telegram.send_plain_message(
            "The email may already be marked read, but the local task status could not be updated. "
            "No later tasks in this command were processed. Check the local application log and resend the command."
        )
        db.set_telegram_update_status(update_id, "failed", error_category="database")
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
