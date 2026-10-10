from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values


APP_NAME = "OutlookTriage"


class ConfigurationError(RuntimeError):
    pass


def _configured_path(env_name: str, fallback: Path) -> Path:
    raw = os.environ.get(env_name)
    return Path(raw).expanduser() if raw else fallback


@dataclass(frozen=True)
class Settings:
    soclaas_api_key: str | None
    soclaas_base_url: str
    model: str | None
    timezone_name: str
    bootstrap_days: int
    overlap_hours: int
    max_body_chars: int
    digest_horizon_days: int
    request_timeout_seconds: float
    config_dir: Path
    data_dir: Path
    state_dir: Path
    outlook_profile: str | None
    secrets_file: Path
    rules_file: Path
    database_file: Path
    reports_dir: Path
    log_file: Path
    lock_dir: Path
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_timeout_seconds: float = 20.0
    telegram_body_preview_chars: int = 6000

    def require_telegram(self, *, require_chat: bool = True) -> None:
        missing: list[str] = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if require_chat and not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ConfigurationError(f"Missing Telegram configuration: {', '.join(missing)}")

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def require_soclaas(self, *, require_model: bool = True) -> None:
        missing = []
        if not self.soclaas_api_key:
            missing.append("SOCLAAS_API_KEY")
        if require_model and not self.model:
            missing.append("EMAIL_TRIAGE_MODEL")
        if missing:
            raise ConfigurationError(f"Missing SoCLaaS configuration: {', '.join(missing)}")


def load_settings() -> Settings:
    home = Path.home()
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    config_dir = _configured_path("OUTLOOK_TRIAGE_CONFIG_DIR", roaming / APP_NAME)
    data_dir = _configured_path("OUTLOOK_TRIAGE_DATA_DIR", local / APP_NAME)
    state_dir = _configured_path("OUTLOOK_TRIAGE_STATE_DIR", data_dir / "state")
    secrets_file = Path(os.environ.get("OUTLOOK_TRIAGE_SECRETS_FILE", config_dir / "secrets.env")).expanduser()

    file_values = dotenv_values(secrets_file) if secrets_file.exists() else {}

    def value(name: str, default: str | None = None) -> str | None:
        result = os.environ.get(name, file_values.get(name, default))
        return str(result).strip() if result is not None and str(result).strip() else None

    timezone_name = value("OUTLOOK_TRIAGE_TIMEZONE", "Asia/Singapore") or "Asia/Singapore"
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ConfigurationError(f"Unknown timezone: {timezone_name}") from exc

    return Settings(
        soclaas_api_key=value("SOCLAAS_API_KEY"),
        telegram_bot_token=value("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=value("TELEGRAM_CHAT_ID"),
        telegram_timeout_seconds=float(value("TELEGRAM_TIMEOUT_SECONDS", "20") or 20),
        telegram_body_preview_chars=int(value("TELEGRAM_BODY_PREVIEW_CHARS", "6000") or 6000),
        soclaas_base_url=(value("SOCLAAS_BASE_URL", "https://soclaas-api.comp.nus.edu.sg/v1") or "").rstrip("/"),
        model=value("EMAIL_TRIAGE_MODEL"),
        timezone_name=timezone_name,
        bootstrap_days=int(value("OUTLOOK_TRIAGE_BOOTSTRAP_DAYS", "7") or 7),
        overlap_hours=int(value("OUTLOOK_TRIAGE_OVERLAP_HOURS", "8") or 8),
        max_body_chars=int(value("OUTLOOK_TRIAGE_MAX_BODY_CHARS", "12000") or 12000),
        digest_horizon_days=int(value("OUTLOOK_TRIAGE_DIGEST_HORIZON_DAYS", "7") or 7),
        request_timeout_seconds=float(value("OUTLOOK_TRIAGE_TIMEOUT_SECONDS", "45") or 45),
        config_dir=config_dir,
        data_dir=data_dir,
        state_dir=state_dir,
        outlook_profile=value("OUTLOOK_PROFILE"),
        secrets_file=secrets_file,
        rules_file=Path(value("OUTLOOK_TRIAGE_RULES_FILE", str(config_dir / "rules.yaml")) or "").expanduser(),
        database_file=Path(value("OUTLOOK_TRIAGE_DATABASE", str(data_dir / "outlook-triage.db")) or "").expanduser(),
        reports_dir=Path(value("OUTLOOK_TRIAGE_REPORTS_DIR", str(data_dir / "reports")) or "").expanduser(),
        log_file=Path(value("OUTLOOK_TRIAGE_LOG_FILE", str(data_dir / "logs" / "outlook-triage.log")) or "").expanduser(),
        lock_dir=Path(value("OUTLOOK_TRIAGE_LOCK_DIR", str(state_dir / "sync.lock")) or "").expanduser(),
    )


def ensure_runtime_directories(settings: Settings) -> None:
    for path in (settings.config_dir, settings.data_dir, settings.state_dir, settings.reports_dir):
        path.mkdir(parents=True, exist_ok=True)

