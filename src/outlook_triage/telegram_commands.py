from __future__ import annotations

import html
import re
from dataclasses import dataclass


HELP_TEXT = (
    "Available commands:\n"
    "/read #<task-id> — mark the original Outlook email as read\n"
    "/show #<task-id> — show a bounded preview without changing read state\n"
    "/help — show this message"
)


@dataclass(frozen=True)
class TelegramCommand:
    action: str
    task_id: int | None = None


_COMMAND_RE = re.compile(
    r"^/(?P<action>read|show|help|start)(?:@[A-Za-z0-9_]+)?(?:\s+(?P<argument>\S+))?\s*$",
    re.IGNORECASE,
)


def parse_command(text: str) -> TelegramCommand | None:
    match = _COMMAND_RE.fullmatch(text.strip())
    if not match:
        return None
    action = match.group("action").lower()
    argument = match.group("argument")
    if action in {"help", "start"}:
        return TelegramCommand("help") if argument is None else None
    if argument is None or not re.fullmatch(r"#?[1-9]\d*", argument):
        return None
    return TelegramCommand(action, int(argument.removeprefix("#")))


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
