from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .database import Database


def _task_line(row) -> str:
    deadline = f" — due {row['deadline']}" if row["deadline"] else ""
    sender = row["sender_name"] or row["sender_address"]
    source = f" ({sender})" if sender else ""
    return f"- [#{row['id']}] {row['description']}{deadline}{source}"


def build_digest(db: Database, settings: Settings, now: datetime | None = None) -> str:
    local_now = now.astimezone(settings.timezone) if now else datetime.now(settings.timezone)
    since = (local_now - timedelta(hours=24)).isoformat(timespec="seconds")
    snapshot = db.digest_snapshot(since)
    today = local_now.date()
    horizon = today + timedelta(days=settings.digest_horizon_days)

    overdue, urgent, upcoming, other, waiting = [], [], [], [], []
    for row in snapshot["tasks"]:
        status = row["status"]
        if status == "waiting":
            waiting.append(row)
            continue
        if status != "open":
            continue
        deadline = date.fromisoformat(row["deadline"]) if row["deadline"] else None
        if deadline and deadline < today:
            overdue.append(row)
        elif row["urgency"] == "urgent" or (deadline and deadline == today):
            urgent.append(row)
        elif deadline and deadline <= horizon:
            upcoming.append(row)
        else:
            other.append(row)

    lines = [f"# Inbox brief — {today.isoformat()}", ""]
    lines.append(f"Last successful Outlook sync: {snapshot['last_sync'] or 'never'}")
    last_run = snapshot["last_run"]
    if not last_run:
        lines.extend(["", "> **Warning:** No synchronization run has completed yet."])
    elif last_run["status"] == "running":
        lines.extend(["", "> **Warning:** The latest synchronization is still running; this digest may be incomplete."])
    elif last_run["status"] != "success":
        detail = last_run.get("error_summary") or "unknown error"
        lines.extend(["", f"> **Warning:** The latest sync failed: {detail}"])
    if (snapshot["counts"].get("failed_count") or 0) > 0:
        lines.extend(["", "> **Warning:** Some messages are pending or failed classification. Run `outlook-triage retry-failed`."])

    sections = (
        ("Overdue", overdue),
        ("Urgent / due today", urgent),
        (f"Due within {settings.digest_horizon_days} days", upcoming),
        ("Other open actions", other),
        ("Waiting / follow-up", waiting),
    )
    for heading, items in sections:
        lines.extend(["", f"## {heading}"])
        lines.extend(_task_line(row) for row in items) if items else lines.append("- None")

    counts = snapshot["counts"]
    lines.extend(
        [
            "",
            "## Last 24 hours",
            f"- Informational: {counts.get('information_count') or 0}",
            f"- Newsletters: {counts.get('newsletter_count') or 0}",
            f"- Automated: {counts.get('automated_count') or 0}",
            f"- Explicitly skipped: {counts.get('skipped_count') or 0}",
            f"- Pending or failed: {counts.get('failed_count') or 0}",
            "",
        ]
    )
    return "\n".join(lines)


def save_digest(text: str, reports_dir: Path, today: date) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"{today.isoformat()}.md"
    target.write_text(text, encoding="utf-8")
    return target

