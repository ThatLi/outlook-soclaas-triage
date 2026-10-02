from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml


QUOTE_MARKERS = (
    re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^From:\s+.+$", re.IGNORECASE),
)
URL_RE = re.compile(r"https?://[^\s<>\])]+", re.IGNORECASE)


@dataclass(frozen=True)
class FilterRules:
    ignored_senders: frozenset[str] = field(default_factory=frozenset)
    ignored_domains: frozenset[str] = field(default_factory=frozenset)
    ignored_subject_patterns: tuple[re.Pattern[str], ...] = ()


def load_rules(path: Path) -> FilterRules:
    if not path.exists():
        return FilterRules()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    patterns = tuple(re.compile(item, re.IGNORECASE) for item in raw.get("ignored_subject_patterns", []))
    return FilterRules(
        ignored_senders=frozenset(str(v).lower() for v in raw.get("ignored_senders", [])),
        ignored_domains=frozenset(str(v).lower() for v in raw.get("ignored_domains", [])),
        ignored_subject_patterns=patterns,
    )


def skip_reason(sender_address: str, subject: str, rules: FilterRules) -> str | None:
    sender = sender_address.strip().lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    if sender in rules.ignored_senders:
        return "ignored sender"
    if domain in rules.ignored_domains:
        return "ignored domain"
    if any(pattern.search(subject or "") for pattern in rules.ignored_subject_patterns):
        return "ignored subject pattern"
    return None


def _remove_url_tracking(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:!?":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) + trailing
    except ValueError:
        return raw + trailing


def clean_body(text: str | None, max_chars: int = 12_000) -> str:
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    kept: list[str] = []
    for line in normalized.splitlines():
        stripped = line.strip()
        if stripped == "--" or any(marker.match(stripped) for marker in QUOTE_MARKERS):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line.rstrip())
    result = "\n".join(kept)
    result = URL_RE.sub(_remove_url_tracking, result)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result[:max_chars]


def sender_details(message: dict) -> tuple[str, str]:
    email_address = (message.get("sender") or {}).get("emailAddress") or {}
    return str(email_address.get("name") or ""), str(email_address.get("address") or "")

