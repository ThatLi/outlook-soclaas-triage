from pathlib import Path

import pytest

from outlook_triage.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        soclaas_api_key="key",
        soclaas_base_url="https://example.test/v1",
        model="test-model",
        timezone_name="Asia/Singapore",
        bootstrap_days=7,
        overlap_hours=8,
        max_body_chars=12000,
        digest_horizon_days=7,
        request_timeout_seconds=10,
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        outlook_profile=None,
        secrets_file=tmp_path / "config" / "secrets.env",
        rules_file=tmp_path / "config" / "rules.yaml",
        database_file=tmp_path / "data" / "outlook-triage.db",
        reports_dir=tmp_path / "data" / "reports",
        log_file=tmp_path / "state" / "app.log",
        lock_dir=tmp_path / "state" / "sync.lock",
        telegram_body_preview_chars=6000,
    )

