from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import datetime, time, timedelta
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import ConfigurationError, Settings
from .database import Database
from .digest import build_digest
from .telegram import format_digest
from .telegram_commands import format_status, format_task_list


RELAY_SCHEMA_VERSION = 1
DELIVERY_HOUR = 8


class RelayError(RuntimeError):
    """A sanitized relay error safe to print or log."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ConfigurationError("OUTLOOK_TRIAGE_RELAY_ENCRYPTION_KEY is not valid base64url") from exc


def relay_key(settings: Settings) -> bytes:
    settings.require_relay()
    key = _b64decode(settings.relay_encryption_key or "")
    if len(key) != 32:
        raise ConfigurationError("OUTLOOK_TRIAGE_RELAY_ENCRYPTION_KEY must encode exactly 32 bytes")
    return key


def next_delivery(now: datetime, settings: Settings) -> datetime:
    local_now = now.astimezone(settings.timezone)
    candidate = datetime.combine(local_now.date(), time(DELIVERY_HOUR), settings.timezone)
    return candidate if local_now < candidate else candidate + timedelta(days=1)


def _canonical_metadata(envelope: dict[str, Any]) -> bytes:
    metadata = {
        "bundle_id": envelope["bundle_id"],
        "generated_at": envelope["generated_at"],
        "intended_date": envelope["intended_date"],
        "last_sync_at": envelope["last_sync_at"],
        "nonce": envelope["nonce"],
        "schema_version": envelope["schema_version"],
    }
    return json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8")


def build_payload(db: Database, settings: Settings, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    delivery = next_delivery(now, settings)
    status = db.operational_status()
    digest_chunks = format_digest(build_digest(db, settings, delivery))
    task_views: dict[str, list[str]] = {}
    for name in ("all", "open", "waiting", "done", "dismissed"):
        rows = db.list_tasks(None if name == "all" else name)
        task_views[name] = format_task_list(rows, name)
    payload = {
        "schema_version": RELAY_SCHEMA_VERSION,
        "digest_chunks": digest_chunks,
        "cached_commands": {
            "list": digest_chunks,
            "status": [format_status(status)],
            "tasks": task_views,
        },
    }
    envelope = {
        "schema_version": RELAY_SCHEMA_VERSION,
        "bundle_id": str(uuid.uuid4()),
        "intended_date": delivery.date().isoformat(),
        "generated_at": now.astimezone(settings.timezone).isoformat(timespec="seconds"),
        "last_sync_at": status.get("last_sync"),
        "nonce": _b64encode(os.urandom(12)),
    }
    return envelope, payload


def encrypt_payload(envelope: dict[str, Any], payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    nonce = _b64decode(str(envelope["nonce"]))
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _canonical_metadata(envelope))
    return {**envelope, "ciphertext": _b64encode(ciphertext)}


def decrypt_payload(envelope: dict[str, Any], key: bytes) -> dict[str, Any]:
    plaintext = AESGCM(key).decrypt(
        _b64decode(str(envelope["nonce"])),
        _b64decode(str(envelope["ciphertext"])),
        _canonical_metadata(envelope),
    )
    return json.loads(plaintext)


class RelayClient:
    def __init__(self, settings: Settings, *, session: requests.Session | None = None):
        settings.require_relay()
        self.base_url = (settings.relay_url or "").rstrip("/")
        self.token = settings.relay_token or ""
        self.timeout = settings.relay_timeout_seconds
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(kwargs.pop("headers", {}))
        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", headers=headers,
                timeout=self.timeout, **kwargs,
            )
        except requests.RequestException as exc:
            raise RelayError("Cloud relay is temporarily unreachable") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise RelayError("Cloud relay is temporarily unavailable")
        if response.status_code >= 400:
            raise RelayError(f"Cloud relay rejected the request (HTTP {response.status_code})", status_code=response.status_code)
        try:
            result = response.json()
        except ValueError as exc:
            raise RelayError("Cloud relay returned an invalid response") from exc
        if not isinstance(result, dict):
            raise RelayError("Cloud relay returned an invalid response")
        return result

    def upload(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/bundles", json=envelope)

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")


def prepare_bundle(db: Database, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    settings.require_relay()
    current = now or datetime.now(settings.timezone)
    metadata, payload = build_payload(db, settings, current)
    envelope = encrypt_payload(metadata, payload, relay_key(settings))
    db.replace_relay_bundle(envelope)
    return envelope


def upload_pending(
    db: Database,
    settings: Settings,
    logger: logging.Logger,
    *,
    client: RelayClient | None = None,
) -> bool:
    envelope = db.pending_relay_bundle()
    if envelope is None:
        return True
    try:
        (client or RelayClient(settings)).upload(envelope)
    except (RelayError, ConfigurationError) as exc:
        db.mark_relay_attempt_failed(str(exc))
        logger.warning("Cloud relay publication remains pending: %s", exc)
        return False
    db.mark_relay_confirmed(str(envelope["bundle_id"]))
    return True


def publish_snapshot(
    db: Database,
    settings: Settings,
    logger: logging.Logger,
    *,
    now: datetime | None = None,
    client: RelayClient | None = None,
) -> tuple[dict[str, Any], bool]:
    envelope = prepare_bundle(db, settings, now=now)
    return envelope, upload_pending(db, settings, logger, client=client)


def publish_if_configured(db: Database, settings: Settings, logger: logging.Logger) -> bool:
    """Publish after a completed operation without changing that operation's result."""
    try:
        if not settings.relay_configured:
            return True
        _, uploaded = publish_snapshot(db, settings, logger)
        return uploaded
    except ConfigurationError as exc:
        logger.warning("Cloud relay configuration is incomplete: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Cloud relay snapshot preparation failed (%s)", type(exc).__name__)
        return False
