from __future__ import annotations

import html
import re
from dataclasses import dataclass


HELP_TEXT = (
    "Available commands:\n"
    "/list — synchronize and show the current digest\n"
    "/sync — synchronize without showing the digest\n"
    "/retry [1-100] — retry failed classifications\n"
    "/status — show synchronization and task health\n"
    "/tasks [open|waiting|done|dismissed|all] — list local tasks\n"
    "/read #<task-id> [#<task-id> ...] — mark one or more original Outlook emails as read\n"
    "/done #<task-id> [#<task-id> ...] — mark emails read and tasks done\n"
    "/dismiss #<task-id> [#<task-id> ...] — mark emails read and tasks dismissed\n"
    "/waiting #<task-id> [#<task-id> ...] — mark tasks waiting\n"
    "/reopen #<task-id> [#<task-id> ...] — reopen tasks\n"
    "/show #<task-id> — show a bounded preview without changing read state\n"
    "/help — show this message"
)

BOT_COMMANDS = (
    ("list", "Synchronize and show the current digest"),
    ("sync", "Synchronize Outlook and classifications"),
    ("retry", "Retry failed classifications"),
    ("status", "Show synchronization and task health"),
    ("tasks", "List tasks, optionally by status"),
    ("read", "Mark one or more emails read"),
    ("done", "Mark emails read and tasks done"),
    ("dismiss", "Mark emails read and tasks dismissed"),
    ("waiting", "Mark one or more tasks waiting"),
    ("reopen", "Reopen one or more tasks"),
    ("show", "Show one email body preview"),
    ("help", "Show command help"),
)

TASK_FILTERS = {"open", "waiting", "done", "dismissed", "all"}


@dataclass(frozen=True)
class TelegramCommand:
    action: str
    task_ids: tuple[int, ...] = ()
    option: str | None = None
    limit: int | None = None

    @property
    def task_id(self) -> int | None:
        return self.task_ids[0] if len(self.task_ids) == 1 else None


_COMMAND_RE = re.compile(
    r"^/(?P<action>list|sync|retry|status|tasks|read|done|dismiss|waiting|reopen|show|help|start)"
    r"(?:@[A-Za-z0-9_]+)?(?:\s+(?P<arguments>.+?))?\s*$",
    re.IGNORECASE,
)


def parse_command(text: str) -> TelegramCommand | None:
    match = _COMMAND_RE.fullmatch(text.strip())
    if not match:
        return None
    action = match.group("action").lower()
    arguments = match.group("arguments")
    if action in {"help", "start"}:
        return TelegramCommand("help") if arguments is None else None
    if action in {"list", "sync", "status"}:
        return TelegramCommand(action) if arguments is None else None
    if action == "retry":
        if arguments is None:
            return TelegramCommand(action, limit=50)
        if not re.fullmatch(r"\d+", arguments):
            return None
        limit = int(arguments)
        return TelegramCommand(action, limit=limit) if 1 <= limit <= 100 else None
    if action == "tasks":
        option = (arguments or "all").lower()
        return TelegramCommand(action, option=option) if option in TASK_FILTERS else None
    if arguments is None:
        return None
    values = arguments.split()
    if not values or any(not re.fullmatch(r"#?[1-9]\d*", value) for value in values):
        return None
    task_ids = tuple(dict.fromkeys(int(value.removeprefix("#")) for value in values))
    if action == "show" and len(task_ids) != 1:
        return None
    return TelegramCommand(action, task_ids)


def format_status(snapshot: dict) -> str:
    last_run = snapshot.get("last_run") or {}
    task_counts = snapshot.get("task_counts") or {}
    return "\n".join(
        (
            "📊 <b>TRIAGE STATUS</b>",
            "",
            f"Last successful sync: <b>{html.escape(str(snapshot.get('last_sync') or 'never'))}</b>",
            f"Latest sync run: <b>{html.escape(str(last_run.get('status') or 'none'))}</b>",
            f"Pending or failed classifications: <b>{int(snapshot.get('pending_or_failed') or 0)}</b>",
            "",
            "<b>TASKS</b>",
            f"Open: <b>{int(task_counts.get('open', 0))}</b>",
            f"Waiting: <b>{int(task_counts.get('waiting', 0))}</b>",
            f"Done: <b>{int(task_counts.get('done', 0))}</b>",
            f"Dismissed: <b>{int(task_counts.get('dismissed', 0))}</b>",
        )
    )


def format_task_list(rows: list, status_filter: str, *, max_plain_chars: int = 3400) -> list[str]:
    title = f"📋 <b>TASKS · {html.escape(status_filter.upper())} · {len(rows)}</b>"
    if not rows:
        return [f"{title}\n\nNo matching tasks."]
    raw_items = []
    for row in rows:
        deadline = f" — due {row['deadline']}" if row["deadline"] else ""
        raw_items.append(
            f"#{row['id']} [{str(row['status']).upper()}] [{str(row['urgency']).upper()}] "
            f"{row['description']}{deadline}"
        )
    body_limit = max(200, max_plain_chars - 200)
    pieces: list[str] = []
    current = ""
    for item in raw_items:
        item_parts = [item[index:index + body_limit] for index in range(0, len(item), body_limit)]
        for part in item_parts:
            candidate = part if not current else f"{current}\n\n{part}"
            if current and len(candidate) > body_limit:
                pieces.append(current)
                current = part
            else:
                current = candidate
    if current:
        pieces.append(current)
    total = len(pieces)
    return [
        f"{title}{'' if total == 1 else f' — PART {index} OF {total}'}\n\n{html.escape(piece)}"
        for index, piece in enumerate(pieces, start=1)
    ]


def format_body_preview(
    subject: str,
    body: str,
    *,
    preview_chars: int = 6000,
    max_plain_chars: int = 3400,
) -> list[str]:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(normalized) > preview_chars
    preview = normalized[:preview_chars] or "(message body is empty)"
    subject_label = subject.strip() or "(no subject)"
    subject_label = subject_label[:300]
    body_limit = max(200, max_plain_chars - 450)
    pieces: list[str] = []
    remaining = preview
    while remaining:
        if len(remaining) <= body_limit:
            pieces.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, body_limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, body_limit + 1)
        if split_at <= 0:
            split_at = body_limit
        pieces.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    total = len(pieces)
    rendered: list[str] = []
    for index, piece in enumerate(pieces, start=1):
        part = "" if total == 1 else f" · PART {index} OF {total}"
        heading = f"📧 <b>EMAIL PREVIEW{part}</b>\n<b>{html.escape(subject_label)}</b>"
        suffix = "\n\n<i>Preview truncated.</i>" if truncated and index == total else ""
        rendered.append(f"{heading}\n\n{html.escape(piece)}{suffix}")
    return rendered
