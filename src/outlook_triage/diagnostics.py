"""Read-only checks. Never open Database, poll updates, or configure logging here."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings, load_settings
from .outlook import OutlookClient
from .relay import RelayClient, relay_key
from .scheduler import inspect_tasks, task_plan
from .soclaas import SoCLaaSClient
from .telegram import TelegramClient


# Reports contain only these fixed texts and bounded codes, never exception strings.
RECOVERY = {
    "configuration": ("Settings cannot be read or required credentials are missing.",
        "Connections and processing cannot start.",
        ("Open Setup and complete the numbered connection steps.",
         "If Setup cannot read settings, ask a trusted administrator to correct the invalid value; keep the file private.",
         "Reopen Troubleshooting; Configuration should say PASS.")),
    "windows": ("This Windows version is not supported.", "Classic Outlook integration needs Windows 10 or 11.",
        ("Use a supported Windows 10 or 11 x64 computer.", "Install classic Outlook and sign in.", "Run Troubleshooting again.")),
    "directories": ("A required settings or data folder is missing or inaccessible.", "Settings, reports or logs may not save.",
        ("Open Setup to create missing folders without replacing existing data.",
         "Ask your administrator to check your account's folder permissions if the problem continues.",
         "Reopen Troubleshooting; Runtime folders should say PASS.")),
    "outlook": ("Classic Outlook or the selected profile is unavailable.", "Mailbox synchronization cannot run.",
        ("Open Outlook and look for the File tab. New Outlook does not offer this integration.",
         "If using New Outlook, open Outlook (classic) from Start, or ask IT to install it. Sign in to the intended mailbox and close blocking dialogs.",
         "Reopen Setup, confirm the displayed profile and mailbox, then run Troubleshooting again.",
         "If access is denied by policy, ask IT to approve access; keep Outlook security protections enabled.")),
    "soclaas": ("SoCLaaS access or the selected model could not be verified.", "New email classifications cannot complete.",
        ("Open Setup and retry the SoCLaaS step. Replace a rejected key using hidden input.",
         "Obtain access through https://soclaas.comp.nus.edu.sg/ on the NUS network; contact your organization if you lack access.",
         "Choose an available chat model. Ask the service administrator which supports structured JSON if unsure.",
         "Check your approved network/VPN/proxy with IT, then rerun Troubleshooting; SoCLaaS should say PASS.")),
    "telegram": ("Telegram rejected the token or the configured private chat is unavailable.", "Bot commands and digest delivery may fail.",
        ("Open the official https://t.me/BotFather and check the token for your own bot.",
         "Open that bot, unblock it if needed, and send /start in a private chat.",
         "Reopen Setup, confirm the intended chat and request the harmless test message.",
         "Run Troubleshooting again; Telegram should say PASS.")),
    "webhook": ("Telegram's webhook does not match the selected operating mode.", "Local polling and webhooks cannot share one bot.",
        ("Open Setup and check Local or Advanced cloud mode.",
         "If the bot belongs to another deployment, leave its webhook unchanged and create a dedicated bot.",
         "For your own cloud deployment, follow the Advanced Cloud guide before deliberately changing its webhook.",
         "Run Troubleshooting again; Webhook should say PASS. Pending updates must be retained.")),
    "cloud": ("The existing cloud deployment or its credentials could not be verified.", "Cloud commands and the Windows bridge may not connect.",
        ("Ask the deployment owner to confirm the HTTPS address and independent secrets.",
         "Open Setup, choose Advanced cloud and retain or re-enter those settings.",
         "Follow the Advanced Cloud guide to verify dataset ownership and the Telegram webhook.",
         "Run Troubleshooting again; Cloud should say PASS. No resources are created by Setup.")),
    "schedules": ("Background tasks are missing, disabled, failing, or point to a different program.", "Automatic processing may not run.",
        ("Open Setup and complete all connection checks and the separate first-run verification.",
         "Choose Enable background operation. If you moved or reinstalled the app, use Setup from the current installation.",
         "Stay signed in, keep classic Outlook available, and use Task Scheduler to check Last Run Result and Next Run Time.",
         "Run Troubleshooting again. PASS confirms registration; send /status to confirm listener response.")),
}
LABELS = dict(configuration="Configuration", windows="Windows", directories="Runtime folders",
              outlook="Classic Outlook/profile", soclaas="SoCLaaS/model", telegram="Telegram",
              webhook="Webhook", cloud="Cloud", schedules="Background tasks")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""


def windows_supported() -> bool:
    return sys.platform == "win32" and sys.getwindowsversion().major >= 10


def safe_failure(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return f"HTTP {status}"
    # No class names from dynamic service responses; no arbitrary exception text.
    return "Check could not complete"


def check_cloud(settings: Settings) -> None:
    from hashlib import sha256
    settings.require_relay()
    relay_key(settings)
    result = RelayClient(settings)._request("GET", "/v2/config")
    if not result.get("cloud_bot_enabled") or not settings.telegram_webhook_secret:
        raise RuntimeError("Cloud configuration incomplete")
    if result.get("webhook_secret_sha256") != sha256(settings.telegram_webhook_secret.encode()).hexdigest():
        raise RuntimeError("Cloud webhook secret mismatch")


def check_webhook(settings: Settings, telegram: TelegramClient) -> None:
    url = telegram.webhook_info().get("url") or ""
    if settings.cloud_bot_enabled:
        settings.require_relay()
        if url != f"{settings.relay_url.rstrip('/')}/telegram/webhook":
            raise RuntimeError("Cloud webhook mismatch")
    elif url:
        raise RuntimeError("Webhook conflicts with local mode")


def collect(settings: Settings, *, schedules=True) -> list[Check]:
    checks = [Check("windows", "PASS" if windows_supported() else "FAIL")]

    def probe(name, action):
        try:
            action()
            checks.append(Check(name, "PASS"))
        except Exception as exc:
            checks.append(Check(name, "FAIL", safe_failure(exc)))

    def configuration():
        settings.require_soclaas()
        settings.require_telegram()
        if not settings.secrets_file.is_file() or not settings.rules_file.is_file():
            raise RuntimeError("Configuration missing")
        from .filters import load_rules
        load_rules(settings.rules_file)

    probe("configuration", configuration)

    def directories():
        for path in (settings.config_dir, settings.data_dir, settings.state_dir,
                     settings.reports_dir, settings.log_file.parent, settings.secrets_file.parent,
                     settings.database_file.parent):
            if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK):
                raise RuntimeError("Runtime folder unavailable")
    probe("directories", directories)

    def outlook():
        with OutlookClient(settings.outlook_profile, timezone=settings.timezone) as client:
            metadata = client.profile_metadata()
            if settings.outlook_profile and metadata["profile"].casefold() != settings.outlook_profile.casefold():
                raise RuntimeError("Outlook is connected to another profile")
    probe("outlook", outlook)
    probe("soclaas", lambda: SoCLaaSClient(settings).verify_model())

    def telegram():
        return TelegramClient(settings.telegram_bot_token or "", settings.telegram_chat_id,
                              timeout=settings.telegram_timeout_seconds)
    def bot():
        settings.require_telegram()
        telegram().identity()
        telegram().private_chat()
    probe("telegram", bot)
    probe("webhook", lambda: check_webhook(settings, telegram()))
    if settings.cloud_bot_enabled:
        probe("cloud", lambda: check_cloud(settings))
    elif any((settings.relay_url, settings.relay_token, settings.relay_encryption_key)):
        def relay():
            settings.require_relay()
            relay_key(settings)
            RelayClient(settings).status()
        probe("cloud", relay)
    if schedules:
        try:
            tasks = inspect_tasks(task_plan(cloud=settings.cloud_bot_enabled))
            good = all(t["registered"] and t["owned"] and t["enabled"] and t["arguments_match"]
                       and t["executable_exists"] and t["last_result"] in (0, 267009, 267011)
                       for t in tasks)
            checks.append(Check("schedules", "PASS" if good else "WARN"))
        except Exception as exc:
            checks.append(Check("schedules", "WARN", safe_failure(exc)))
    return checks


def display(checks: list[Check]) -> None:
    for check in checks:
        print(f"\n{check.status}: {LABELS[check.name]}" + (f" ({check.detail})" if check.detail else ""))
        if check.status != "PASS":
            problem, effect, steps = RECOVERY[check.name]
            print(f"{problem}\nEffect: {effect}")
            for index, step in enumerate(steps, 1):
                print(f"  {index}. {step}")
    print("\nThese checks did not send messages, read email items, consume Telegram updates, open or migrate SQLite, or change schedules.")
    print("Folder permissions were inspected without a write test. Outlook may open or ask for permission; no security prompts are bypassed.")
    print("A model listing cannot prove classification quality. A chat lookup cannot prove delivery. Cloud dataset ownership is not tested by publishing.")
    print("Task registration does not prove a future run. No test messages or mailbox requests were sent.")


def save_report(checks: list[Check], directory: Path) -> Path:
    # Whitelist both structure and text, including when called by other code.
    payload = {"application": "Outlook SoCLaaS Triage", "version": __version__,
               "created_utc": datetime.now(timezone.utc).isoformat(),
               "checks": [{"name": c.name, "status": c.status,
                           "detail": c.detail if c.detail == "Check could not complete" or
                           (len(c.detail) == 8 and c.detail.startswith("HTTP ") and c.detail[5:].isdigit()) else ""}
                          for c in checks if c.name in RECOVERY and c.status in {"PASS", "FAIL", "WARN"}]}
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("diagnostic-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + ".json")
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def doctor(*, interactive=False) -> int:
    print("Outlook Triage Troubleshooting - checking connections; this can take a few minutes.")
    settings = None
    try:
        settings = load_settings()
        checks = collect(settings)
    except Exception as exc:
        checks = [Check("configuration", "FAIL", safe_failure(exc))]
    display(checks)
    if interactive:
        print("\nOptional report: app version, UTC time, check names, PASS/FAIL/WARN and safe HTTP codes only.")
        print("It excludes credentials, messages, email addresses, chat IDs, profile names and personal paths. Nothing is uploaded.")
        if input("1 Save this report locally; 2 Finish [2]: ").strip() == "1":
            if settings:
                directory = settings.reports_dir
            else:
                directory = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "OutlookTriage/reports"
            try:
                target = save_report(checks, directory)
                print(f"Saved: {target}\nOpen File Explorer and paste the folder path above to find it.")
            except OSError:
                print("The report could not be saved. Ask IT to check your folder permissions, then retry.")
    return 1 if any(c.status == "FAIL" for c in checks) else 2 if any(c.status == "WARN" for c in checks) else 0
