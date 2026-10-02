from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigurationError, Settings, ensure_runtime_directories, load_settings
from .database import Database
from .digest import build_digest, save_digest
from .filters import clean_body, load_rules, sender_details
from .locking import AlreadyRunning, process_lock
from .logging_utils import configure_logging
from .models import EmailForClassification
from .outlook import OutlookClient
from .service import retry_failed, synchronize
from .soclaas import SoCLaaSClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="outlook-triage", description="Read-only Outlook and SoCLaaS email triage")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create configuration templates and runtime directories")
    commands.add_parser("models", help="List SoCLaaS models available to this API key")

    check = commands.add_parser("check-mail", help="Print recent message metadata without bodies")
    check.add_argument("--limit", type=int, default=10)

    classify = commands.add_parser("classify-one", help="Classify one selected Outlook item without saving it")
    classify.add_argument("outlook_entry_id")

    commands.add_parser("sync", help="Read new classic Outlook messages and classify them")
    retry = commands.add_parser("retry-failed", help="Retry pending or failed messages")
    retry.add_argument("--limit", type=int, default=50)
    commands.add_parser("digest", help="Print and save today's deterministic digest")

    tasks = commands.add_parser("tasks", help="List or update local task state")
    task_commands = tasks.add_subparsers(dest="task_command", required=True)
    task_list = task_commands.add_parser("list")
    task_list.add_argument("--status", choices=["open", "done", "dismissed", "waiting"])
    for action in ("done", "waiting", "dismiss", "reopen"):
        action_parser = task_commands.add_parser(action)
        action_parser.add_argument("task_id", type=int)
    return parser


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def initialize(settings: Settings) -> None:
    ensure_runtime_directories(settings)
    secrets = """# Keep this file private.
SOCLAAS_API_KEY=
SOCLAAS_BASE_URL=https://soclaas-api.comp.nus.edu.sg/v1
EMAIL_TRIAGE_MODEL=
OUTLOOK_PROFILE=
OUTLOOK_TRIAGE_TIMEZONE=Asia/Singapore
OUTLOOK_TRIAGE_BOOTSTRAP_DAYS=7
OUTLOOK_TRIAGE_OVERLAP_HOURS=8
OUTLOOK_TRIAGE_MAX_BODY_CHARS=12000
OUTLOOK_TRIAGE_DIGEST_HORIZON_DAYS=7
"""
    rules = """ignored_senders: []
ignored_domains: []
ignored_subject_patterns: []
"""
    created = []
    if _write_if_missing(settings.secrets_file, secrets):
        created.append(settings.secrets_file)
    if _write_if_missing(settings.rules_file, rules):
        created.append(settings.rules_file)
    Database(settings.database_file).close()
    if created:
        print("Created:")
        for path in created:
            print(f"  {path}")
    else:
        print("Configuration files already exist; nothing was overwritten.")
    print(f"Database ready: {settings.database_file}")


def _format_sender(message: dict) -> str:
    name, address = sender_details(message)
    return f"{name} <{address}>" if name and address else address or name or "unknown"


def _run_tasks(args, db: Database) -> int:
    if args.task_command == "list":
        rows = db.list_tasks(args.status)
        if not rows:
            print("No matching tasks.")
            return 0
        for row in rows:
            deadline = row["deadline"] or "no deadline"
            print(f"#{row['id']} [{row['status']}] [{row['urgency']}] {row['description']} — {deadline}")
        return 0
    statuses = {"done": "done", "waiting": "waiting", "dismiss": "dismissed", "reopen": "open"}
    if not db.set_task_status(args.task_id, statuses[args.task_command]):
        print(f"Task #{args.task_id} was not found.", file=sys.stderr)
        return 1
    print(f"Task #{args.task_id} marked {statuses[args.task_command]}.")
    return 0


def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    ensure_runtime_directories(settings)
    logger = configure_logging(settings.log_file, args.verbose)

    if args.command == "init":
        initialize(settings)
        return 0

    if args.command == "models":
        ai = SoCLaaSClient(settings)
        models = ai.list_models()
        for item in models:
            model_id = item.get("id", "unknown")
            context = item.get("context_length") or item.get("context_window") or item.get("max_model_len")
            print(f"{model_id}" + (f" (context: {context})" if context else ""))
        return 0

    if args.command == "check-mail":
        with OutlookClient(settings.outlook_profile, timezone=settings.timezone) as outlook:
            messages = outlook.newest_messages(args.limit)
        for message in messages:
            print(f"{message.get('receivedDateTime', '')} | {_format_sender(message)} | {message.get('subject') or '(no subject)'}")
            print(f"  EntryID: {message['sourceId']}")
        return 0

    if args.command == "classify-one":
        ai = SoCLaaSClient(settings)
        ai.verify_model()
        with OutlookClient(settings.outlook_profile, timezone=settings.timezone) as outlook:
            message = outlook.get_message(args.outlook_entry_id)
        body = clean_body(
            ((message.get("body") or {}).get("content") or message.get("bodyPreview") or ""),
            settings.max_body_chars,
        )
        result = ai.classify(
            EmailForClassification(
                source_id=args.outlook_entry_id,
                sender=_format_sender(message),
                subject=message.get("subject") or "(no subject)",
                received_at=message.get("receivedDateTime") or "",
                body=body or "(message body was empty)",
            ),
            datetime.now(settings.timezone),
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        print("Classification was not saved. Run `sync` to persist inbox results.")
        return 0

    db = Database(settings.database_file)
    try:
        if args.command == "tasks":
            return _run_tasks(args, db)
        if args.command == "digest":
            now = datetime.now(settings.timezone)
            text = build_digest(db, settings, now)
            target = save_digest(text, settings.reports_dir, now.date())
            print(text)
            print(f"Saved: {target}")
            return 0
        if args.command in {"sync", "retry-failed"}:
            ai = SoCLaaSClient(settings)
            rules = load_rules(settings.rules_file)
            with process_lock(settings.lock_dir):
                with OutlookClient(settings.outlook_profile, timezone=settings.timezone) as outlook:
                    if args.command == "sync":
                        counts = synchronize(
                            db=db, outlook=outlook, ai=ai, rules=rules, settings=settings, logger=logger
                        )
                    else:
                        counts = retry_failed(
                            db=db, outlook=outlook, ai=ai, rules=rules, settings=settings,
                            logger=logger, limit=args.limit,
                        )
            print(" ".join(f"{key}={value}" for key, value in counts.items()))
            return 0 if counts["failed"] == 0 else 2
    finally:
        db.close()
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (ConfigurationError, AlreadyRunning, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

