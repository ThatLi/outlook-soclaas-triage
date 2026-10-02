from __future__ import annotations

import logging
from datetime import datetime

from .config import Settings
from .database import Database
from .filters import FilterRules, clean_body, sender_details, skip_reason
from .models import EmailForClassification
from .outlook import OutlookClient
from .soclaas import SoCLaaSClient


def _sender_label(message: dict) -> str:
    name, address = sender_details(message)
    if name and address:
        return f"{name} <{address}>"
    return address or name or "unknown sender"


def process_message(
    message: dict,
    *,
    db: Database,
    ai: SoCLaaSClient,
    rules: FilterRules,
    settings: Settings,
    logger: logging.Logger,
) -> str:
    key = str(message["messageKey"])
    status = db.email_status(key)
    if status in {"processed", "skipped"}:
        return "unchanged"

    _, sender_address = sender_details(message)
    reason = skip_reason(sender_address, str(message.get("subject") or ""), rules)
    if reason:
        db.mark_skipped(key, reason)
        logger.info("Skipped Outlook item %s: %s", message["sourceId"][-12:], reason)
        return "skipped"

    body = clean_body(
        ((message.get("body") or {}).get("content") or message.get("bodyPreview") or ""),
        settings.max_body_chars,
    )
    email = EmailForClassification(
        source_id=message["sourceId"],
        sender=_sender_label(message),
        subject=str(message.get("subject") or "(no subject)"),
        received_at=str(message.get("receivedDateTime") or ""),
        body=body or "(message body was empty)",
    )
    try:
        result = ai.classify(email, datetime.now(settings.timezone))
        db.save_classification(key, result, settings.model or "")
        logger.info("Classified Outlook item %s as %s", message["sourceId"][-12:], result.category)
        return "processed"
    except Exception as exc:
        db.mark_failed(key, str(exc))
        logger.error("Classification failed for Outlook item %s: %s", message["sourceId"][-12:], exc)
        return "failed"


def synchronize(
    *,
    db: Database,
    outlook: OutlookClient,
    ai: SoCLaaSClient,
    rules: FilterRules,
    settings: Settings,
    logger: logging.Logger,
) -> dict[str, int]:
    run_id = db.start_run("sync")
    counts = {"processed": 0, "skipped": 0, "failed": 0, "unchanged": 0}
    try:
        ai.verify_model()
        result = outlook.sync(
            db.get_last_received_at(),
            bootstrap_days=settings.bootstrap_days,
            overlap_hours=settings.overlap_hours,
        )
        db.store_sync_batch(result.messages, result.high_water_received_at)
        for message in result.messages:
            outcome = process_message(
                message,
                db=db,
                ai=ai,
                rules=rules,
                settings=settings,
                logger=logger,
            )
            counts[outcome] += 1
        db.finish_run(
            run_id,
            success=counts["failed"] == 0,
            processed=counts["processed"],
            skipped=counts["skipped"],
            failed=counts["failed"],
            error="one or more classifications failed" if counts["failed"] else None,
        )
        return counts
    except Exception as exc:
        db.finish_run(
            run_id,
            success=False,
            processed=counts["processed"],
            skipped=counts["skipped"],
            failed=counts["failed"],
            error=str(exc)[:1000],
        )
        raise


def retry_failed(
    *,
    db: Database,
    outlook: OutlookClient,
    ai: SoCLaaSClient,
    rules: FilterRules,
    settings: Settings,
    logger: logging.Logger,
    limit: int = 50,
) -> dict[str, int]:
    run_id = db.start_run("retry-failed")
    counts = {"processed": 0, "skipped": 0, "failed": 0, "unchanged": 0}
    try:
        pending = db.pending_messages(limit)
        if pending:
            ai.verify_model()
        for row in pending:
            try:
                message = outlook.get_message(row["source_id"], row["store_id"])
                outcome = process_message(
                    message,
                    db=db,
                    ai=ai,
                    rules=rules,
                    settings=settings,
                    logger=logger,
                )
            except Exception as exc:
                db.mark_failed(row["message_key"], str(exc))
                logger.error("Retry failed for Outlook item %s: %s", row["source_id"][-12:], exc)
                outcome = "failed"
            counts[outcome] += 1
        db.finish_run(
            run_id,
            success=counts["failed"] == 0,
            processed=counts["processed"],
            skipped=counts["skipped"],
            failed=counts["failed"],
            error="one or more messages failed again" if counts["failed"] else None,
        )
        return counts
    except Exception as exc:
        db.finish_run(run_id, success=False, error=str(exc)[:1000])
        raise

