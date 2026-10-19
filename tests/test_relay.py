from __future__ import annotations

import base64
import logging
from dataclasses import replace
from datetime import datetime

import pytest
from cryptography.exceptions import InvalidTag

from outlook_triage.config import ConfigurationError
from outlook_triage.database import Database
from outlook_triage.relay import (
    build_payload,
    decrypt_payload,
    encrypt_payload,
    next_delivery,
    prepare_bundle,
    upload_pending,
)


KEY_TEXT = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
FIXTURE_CIPHERTEXT = (
    "PCC1eqaNp3_SIvjm3IgWCfD0vU_SFzYPTEXf3j9VYoxnedaI2rN3uBDNGIj78xQXjGdC0Hb00K5e419qOtmuzINIpw6mogQ8MHbeD5zkfcoC8esDGg9xUqagsu4cmcrgoosqg8Bj8kHDMBzWJxtEXYA8P4fBqlX_I91tlt_KTHnaO3Sv2rDpaVO8NdwR-CirUZGQQWpy-z_iNJtVwInL0vGp-HyRi7kMwkO5k7jl9c-h4agcqfZfnha8qKIcpOiFdj75k2Kz9PMGK-Sa7-TjBXeobBlP8CJTYBI"
)


def configured(settings):
    return replace(
        settings,
        relay_url="https://relay.example.test",
        relay_token="upload-secret",
        relay_encryption_key=KEY_TEXT,
    )


def test_relay_configuration_is_all_or_nothing(settings):
    assert settings.relay_configured is False
    partial = replace(settings, relay_url="https://relay.example.test")
    with pytest.raises(ConfigurationError, match="RELAY_TOKEN"):
        _ = partial.relay_configured
    invalid = replace(configured(settings), relay_encryption_key="AA")
    db = Database(invalid.database_file)
    try:
        with pytest.raises(ConfigurationError, match="32 bytes"):
            prepare_bundle(db, invalid)
    finally:
        db.close()


def test_next_delivery_uses_strict_0800_boundary(settings):
    before = datetime(2026, 10, 11, 7, 59, tzinfo=settings.timezone)
    at = datetime(2026, 10, 11, 8, 0, tzinfo=settings.timezone)
    assert next_delivery(before, settings).isoformat() == "2026-10-11T08:00:00+08:00"
    assert next_delivery(at, settings).isoformat() == "2026-10-12T08:00:00+08:00"


def test_snapshot_contains_encrypted_phase_two_command_views(settings):
    configured_settings = configured(settings)
    db = Database(configured_settings.database_file)
    try:
        now = datetime(2026, 10, 10, 23, 50, tzinfo=settings.timezone)
        metadata, payload = build_payload(db, configured_settings, now)
        envelope = encrypt_payload(metadata, payload, bytes(range(32)))
        assert "digest_chunks" not in envelope
        decrypted = decrypt_payload(envelope, bytes(range(32)))
        assert set(decrypted["cached_commands"]["tasks"]) == {
            "all", "open", "waiting", "done", "dismissed"
        }
        assert decrypted["cached_commands"]["list"] == decrypted["digest_chunks"]
        tampered = {**envelope, "intended_date": "2026-10-12"}
        with pytest.raises(InvalidTag):
            decrypt_payload(tampered, bytes(range(32)))
    finally:
        db.close()


def test_python_encryption_matches_worker_fixture():
    envelope = {
        "schema_version": 1,
        "bundle_id": "9c36de71-c074-44f3-bc5e-07e9489c5147",
        "intended_date": "2026-10-11",
        "generated_at": "2026-10-10T23:50:00+08:00",
        "last_sync_at": "2026-10-10T23:49:58+08:00",
        "nonce": "AAECAwQFBgcICQoL",
    }
    payload = {
        "schema_version": 1,
        "digest_chunks": ["<b>fixture digest</b>"],
        "cached_commands": {
            "list": ["<b>fixture digest</b>"],
            "status": ["status"],
            "tasks": {name: [] for name in ("all", "open", "waiting", "done", "dismissed")},
        },
    }
    assert encrypt_payload(envelope, payload, bytes(range(32)))["ciphertext"] == FIXTURE_CIPHERTEXT


def test_outbox_replaces_pending_bundle_and_confirms_upload(settings):
    configured_settings = configured(settings)
    db = Database(configured_settings.database_file)

    class Client:
        def __init__(self):
            self.uploads = []

        def upload(self, envelope):
            self.uploads.append(envelope)
            return {"status": "stored"}

    try:
        first = prepare_bundle(
            db, configured_settings,
            now=datetime(2026, 10, 10, 19, 50, tzinfo=settings.timezone),
        )
        second = prepare_bundle(
            db, configured_settings,
            now=datetime(2026, 10, 10, 23, 50, tzinfo=settings.timezone),
        )
        assert first["bundle_id"] != second["bundle_id"]
        assert db.pending_relay_bundle()["bundle_id"] == second["bundle_id"]
        client = Client()
        assert upload_pending(db, configured_settings, logging.getLogger(), client=client)
        assert client.uploads[0]["bundle_id"] == second["bundle_id"]
        assert db.pending_relay_bundle() is None
        assert db.relay_bundle()["status"] == "confirmed"
    finally:
        db.close()
