from __future__ import annotations

import html
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


class TelegramError(RuntimeError):
    """A sanitized Telegram delivery error safe to show or log."""


@dataclass(frozen=True)
class TelegramChat:
    chat_id: str
    display_name: str
    chat_type: str


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


def format_digest(markdown: str, *, max_plain_chars: int = 3400) -> list[str]:
    """Render the digest Markdown subset as safe, bounded Telegram HTML."""
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
