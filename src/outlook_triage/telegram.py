from __future__ import annotations

import html
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

import requests


class TelegramError(RuntimeError):
    """A sanitized Telegram delivery error safe to show or log."""


@dataclass(frozen=True)
class TelegramChat:
    chat_id: str
    display_name: str
    chat_type: str


@dataclass(frozen=True)
class _DigestSection:
    heading: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _DigestTask:
    task_id: str
    description: str
    deadline: str | None = None
    sender: str | None = None


_PRIORITY_HEADINGS = (
    "Overdue",
    "Urgent / due today",
    "Other open actions",
    "Waiting / follow-up",
)
_TASK_RE = re.compile(r"^- \[#(?P<id>\d+)] (?P<body>.*)$")
_DUE_RE = re.compile(r"^(?P<description>.*) — due (?P<deadline>\d{4}-\d{2}-\d{2})(?: \((?P<sender>.*)\))?$")
_SENDER_RE = re.compile(r"^(?P<description>.*) \((?P<sender>[^()]*)\)$")
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_length(rendered_html: str) -> int:
    return len(html.unescape(_TAG_RE.sub("", rendered_html)))


def _is_priority_heading(heading: str) -> bool:
    return heading in _PRIORITY_HEADINGS or bool(re.fullmatch(r"Due within \d+ days", heading))


def _parse_outlook_digest(markdown: str) -> tuple[str, list[str], list[_DigestSection]] | None:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
    if not lines or not re.fullmatch(r"# Inbox brief — \d{4}-\d{2}-\d{2}", lines[0].strip()):
        return None

    preamble: list[str] = []
    sections: list[_DigestSection] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_heading is not None:
                sections.append(_DigestSection(current_heading, tuple(current_lines)))
            current_heading = stripped[3:].strip()
            current_lines = []
        elif current_heading is None:
            if stripped:
                preamble.append(stripped)
        elif stripped:
            current_lines.append(stripped)
    if current_heading is not None:
        sections.append(_DigestSection(current_heading, tuple(current_lines)))
    if not any(_is_priority_heading(section.heading) for section in sections):
        return None
    return lines[0].strip(), preamble, sections


def _parse_task(line: str) -> _DigestTask:
    matched = _TASK_RE.match(line)
    if not matched:
        return _DigestTask("", line.removeprefix("- "))
    body = matched.group("body")
    due = _DUE_RE.match(body)
    if due:
        return _DigestTask(
            matched.group("id"),
            due.group("description"),
            due.group("deadline"),
            due.group("sender"),
        )
    sender = _SENDER_RE.match(body)
    if sender:
        return _DigestTask(matched.group("id"), sender.group("description"), sender=sender.group("sender"))
    return _DigestTask(matched.group("id"), body)


def _display_date(value: str, *, today: str | None = None) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    if today and value == today:
        return "today"
    return f"{parsed.day} {parsed.strftime('%b')}"


def _render_task(task: _DigestTask, *, today: str, continuation: bool = False) -> str:
    marker = "↳" if continuation else "☐"
    task_id = f" <code>#{html.escape(task.task_id)}</code>" if task.task_id else ""
    suffix = " <i>(continued)</i>" if continuation else ""
    first_line = f"{marker}{task_id}{suffix} {html.escape(task.description)}".rstrip()
    metadata: list[str] = []
    if task.deadline:
        metadata.append(f"Due <b>{html.escape(_display_date(task.deadline, today=today))}</b>")
    if task.sender:
        metadata.append(html.escape(task.sender))
    return first_line if not metadata else f"{first_line}\n {' · '.join(metadata)}"


def _split_task(task: _DigestTask, *, today: str, limit: int) -> list[str]:
    rendered = _render_task(task, today=today)
    if _visible_length(rendered) <= limit:
        return [rendered]

    # Reserve room for the marker, task ID, continuation label, and final metadata.
    description_limit = max(20, limit - len(task.task_id) - 80)
    pieces = _split_plain_block(task.description, description_limit)
    rendered_pieces: list[str] = []
    for index, piece in enumerate(pieces):
        fragment = _DigestTask(
            task.task_id,
            piece,
            task.deadline if index == len(pieces) - 1 else None,
            task.sender if index == len(pieces) - 1 else None,
        )
        rendered_pieces.append(_render_task(fragment, today=today, continuation=index > 0))
    return rendered_pieces


def _section_style(heading: str) -> tuple[str, str]:
    if heading == "Overdue":
        return "🔴", "OVERDUE"
    if heading == "Urgent / due today":
        return "🟠", "URGENT / DUE TODAY"
    if heading.startswith("Due within "):
        return "🟡", heading.upper()
    if heading == "Other open actions":
        return "⚪", "OTHER OPEN ACTIONS"
    return "💤", "WAITING / FOLLOW-UP"


def _section_items(section: _DigestSection, *, today: str, limit: int) -> list[str]:
    lines = [line for line in section.lines if line != "- None"]
    items: list[str] = []
    for line in lines:
        items.extend(_split_task(_parse_task(line), today=today, limit=max(20, limit - 80)))
    return items


def _render_section_parts(section: _DigestSection, *, today: str, limit: int) -> list[str]:
    items = _section_items(section, today=today, limit=limit)
    if not items:
        return []
    emoji, label = _section_style(section.heading)
    base_heading = f"{emoji} <b>{label} · {len([line for line in section.lines if line != '- None'])}</b>"
    conservative_heading = f"{base_heading} — PART 999 OF 999"
    groups: list[list[str]] = []
    current: list[str] = []
    for item in items:
        candidate_items = "\n\n".join([*current, item])
        candidate = f"{conservative_heading}\n\n{candidate_items}"
        if current and _visible_length(candidate) > limit:
            groups.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        groups.append(current)

    total = len(groups)
    rendered: list[str] = []
    for index, group in enumerate(groups, start=1):
        heading = base_heading if total == 1 else f"{base_heading} — PART {index} OF {total}"
        group_text = "\n\n".join(group)
        rendered.append(f"{heading}\n\n{group_text}")
    return rendered


def _pack_sections(parts: list[list[str]], *, limit: int) -> list[str]:
    messages: list[str] = []
    current = ""
    for section_parts in parts:
        if len(section_parts) > 1:
            if current:
                messages.append(current)
                current = ""
            messages.extend(section_parts)
            continue
        if not section_parts:
            continue
        part = section_parts[0]
        candidate = part if not current else f"{current}\n\n{part}"
        if current and _visible_length(candidate) > limit:
            messages.append(current)
            current = part
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _format_sync_value(value: str) -> str:
    if value.lower() == "never":
        return "never"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d %b, %H:%M")


def _format_outlook_digest(markdown: str, *, limit: int) -> list[str] | None:
    parsed = _parse_outlook_digest(markdown)
    if parsed is None:
        return None
    title, preamble, sections = parsed
    today = title.rsplit(" — ", 1)[1]
    parsed_today = date.fromisoformat(today)
    rendered_title = f"📬 <b>INBOX BRIEF · {parsed_today.day} {parsed_today.strftime('%b %Y').upper()}</b>"

    priorities = [section for section in sections if _is_priority_heading(section.heading)]
    counts = {section.heading: len([line for line in section.lines if line != "- None"]) for section in priorities}
    upcoming = next((section for section in priorities if section.heading.startswith("Due within ")), None)
    horizon_label = upcoming.heading if upcoming else "Due within horizon"
    open_actions = "\n".join(
        [
            "<b>OPEN ACTIONS</b>",
            "",
            f"🔴 Overdue: <b>{counts.get('Overdue', 0)}</b>",
            f"🟠 Urgent / due today: <b>{counts.get('Urgent / due today', 0)}</b>",
            f"🟡 {html.escape(horizon_label)}: <b>{counts.get(upcoming.heading, 0) if upcoming else 0}</b>",
            f"⚪ Other open actions: <b>{counts.get('Other open actions', 0)}</b>",
            f"💤 Waiting / follow-up: <b>{counts.get('Waiting / follow-up', 0)}</b>",
        ]
    )

    activity = next((section for section in sections if section.heading == "Last 24 hours"), None)
    activity_lines = ["📊 <b>LAST 24 HOURS</b>", ""]
    if activity:
        for line in activity.lines:
            content = line.removeprefix("- ")
            label, separator, value = content.partition(":")
            activity_lines.append(
                f"• {html.escape(label)}: <b>{html.escape(value.strip())}</b>" if separator else f"• {html.escape(content)}"
            )

    last_sync = next((line for line in preamble if line.startswith("Last successful Outlook sync:")), None)
    sync_lines = ["🔄 <b>SYNCHRONIZATION</b>", ""]
    if last_sync:
        sync_value = last_sync.split(":", 1)[1].strip()
        sync_lines.append(f"✅ Last successful sync: <b>{html.escape(_format_sync_value(sync_value))}</b>")
    for line in preamble:
        if not line.startswith(">"):
            continue
        warning = line.lstrip(">").strip().replace("**", "").replace("`", "")
        sync_lines.append(f"⚠️ {html.escape(warning.removeprefix('Warning:').strip())}")

    summary = "\n\n".join((rendered_title, open_actions, "\n".join(activity_lines), "\n".join(sync_lines)))
    messages = [summary]

    section_by_heading = {section.heading: section for section in priorities}
    upcoming_heading = upcoming.heading if upcoming else None
    immediate_order = ["Overdue", "Urgent / due today"] + ([upcoming_heading] if upcoming_heading else [])
    immediate_parts = [
        _render_section_parts(section_by_heading[heading], today=today, limit=limit)
        for heading in immediate_order
        if heading in section_by_heading
    ]
    messages.extend(_pack_sections(immediate_parts, limit=limit))

    # Keep these two rare sections together when possible, and never use spare
    # space in the upcoming-deadlines message for "Other open actions".
    rare_parts = [
        _render_section_parts(section_by_heading[heading], today=today, limit=limit)
        for heading in ("Other open actions", "Waiting / follow-up")
        if heading in section_by_heading
    ]
    messages.extend(_pack_sections(rare_parts, limit=limit))
    return [message for message in messages if message.strip()]


def _render_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith("#"):
        return f"<b>{html.escape(stripped.lstrip('#').strip())}</b>"
    if stripped.startswith(">"):
        warning = stripped.lstrip(">").strip().replace("**", "").replace("`", "")
        return f"<b>Warning:</b> {html.escape(warning.removeprefix('Warning:').strip())}"
    if stripped.startswith("- "):
        return f"• {html.escape(stripped[2:].replace('`', ''))}"
    return html.escape(stripped.replace("**", "").replace("`", ""))


def _split_plain_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    pieces: list[str] = []
    remaining = block
    while remaining:
        if len(remaining) <= limit:
            pieces.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        pieces.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return [piece for piece in pieces if piece]


def _format_generic_digest(markdown: str, *, max_plain_chars: int) -> list[str]:
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        normalized = "Outlook triage digest\n\nNo digest content was generated."
    blocks = [block.strip() for block in re.split(r"\n\s*\n", normalized) if block.strip()]
    plain_chunks: list[str] = []
    current = ""
    for block in blocks:
        for piece in _split_plain_block(block, max_plain_chars):
            candidate = piece if not current else f"{current}\n\n{piece}"
            if len(candidate) <= max_plain_chars:
                current = candidate
            else:
                if current:
                    plain_chunks.append(current)
                current = piece
    if current:
        plain_chunks.append(current)
    return [
        rendered
        for chunk in plain_chunks
        if (rendered := "\n".join(_render_line(line) for line in chunk.splitlines()).strip())
    ]


def format_digest(markdown: str, *, max_plain_chars: int = 3400) -> list[str]:
    """Render safe, bounded Telegram HTML with a structured Outlook layout."""
    outlook_chunks = _format_outlook_digest(markdown, limit=max_plain_chars)
    if outlook_chunks is not None:
        return outlook_chunks
    return _format_generic_digest(markdown, max_plain_chars=max_plain_chars)


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str | None = None,
        *,
        timeout: float = 20.0,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
        self._token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep = sleep

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    @staticmethod
    def _description(payload: Any, fallback: str) -> str:
        if not isinstance(payload, dict):
            return fallback
        description = str(payload.get("description") or fallback).lower()
        if "token" in description or "unauthorized" in description:
            return "Telegram rejected the bot credentials"
        if "chat not found" in description:
            return "Telegram could not find the configured private chat"
        if "bot was blocked" in description or "forbidden" in description:
            return "Telegram delivery is forbidden or the bot was blocked"
        return fallback

    def _request(self, method: str, data: dict[str, Any] | None = None) -> Any:
        for attempt in range(5):
            try:
                response = self.session.post(self._url(method), data=data or {}, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == 4:
                    raise TelegramError("Telegram was unreachable after retries") from exc
                self.sleep(min(30.0, 2**attempt) + random.uniform(0, 0.5))
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                if response.status_code >= 500 and attempt < 4:
                    self.sleep(min(30.0, 2**attempt) + random.uniform(0, 0.5))
                    continue
                raise TelegramError("Telegram returned an invalid response") from exc
            if response.status_code == 429:
                if attempt == 4:
                    raise TelegramError("Telegram rate limiting persisted after retries")
                parameters = payload.get("parameters", {}) if isinstance(payload, dict) else {}
                retry_after = parameters.get("retry_after", 2**attempt)
                try:
                    delay = max(0.0, min(60.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = float(2**attempt)
                self.sleep(delay)
                continue
            if response.status_code >= 500:
                if attempt == 4:
                    raise TelegramError("Telegram server error persisted after retries")
                self.sleep(min(30.0, 2**attempt) + random.uniform(0, 0.5))
                continue
            if response.status_code in {400, 401, 403} or not isinstance(payload, dict) or not payload.get("ok"):
                raise TelegramError(self._description(payload, f"Telegram rejected the request (HTTP {response.status_code})"))
            return payload.get("result")
        raise AssertionError("unreachable")

    def recent_private_chats(self) -> list[TelegramChat]:
        result = self._request("getUpdates", {"limit": 100, "timeout": 0})
        chats: dict[str, TelegramChat] = {}
        for update in result if isinstance(result, list) else []:
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            if chat.get("type") != "private" or "id" not in chat:
                continue
            chat_id = str(chat["id"])
            name = " ".join(str(chat.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
            if not name:
                name = str(chat.get("username") or "Private chat")
            chats[chat_id] = TelegramChat(chat_id, name, "private")
        return list(chats.values())

    def send_message(self, text: str, *, chat_id: str | None = None) -> None:
        target = chat_id or self.chat_id
        if not target:
            raise TelegramError("TELEGRAM_CHAT_ID is not configured")
        self._request("sendMessage", {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })

    def send_digest(self, markdown: str) -> int:
        chunks = format_digest(markdown)
        for index, chunk in enumerate(chunks, start=1):
            try:
                self.send_message(chunk)
            except TelegramError as exc:
                raise TelegramError(f"Telegram digest delivery failed at message {index} of {len(chunks)}: {exc}") from exc
        return len(chunks)
