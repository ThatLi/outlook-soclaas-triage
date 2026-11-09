"""Numbered console flows; all email processing remains in the existing CLI."""
from __future__ import annotations

import hashlib
import json
import os
import warnings
from dataclasses import replace
from datetime import datetime
from getpass import GetPassWarning, getpass

from .config import ConfigurationError, Settings, load_settings, save_settings
from .diagnostics import Check, check_cloud, check_webhook, collect, display, safe_failure, windows_supported
from .outlook import OutlookClient
from .soclaas import SoCLaaSClient
from .telegram import TelegramClient, TelegramError
from . import scheduler


class Back(Exception):
    pass


class Exit(Exception):
    pass


def answer(prompt: str, *, secret=False) -> str:
    if secret:
        # Refuse getpass's echoing fallback if a console cannot hide the secret.
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            value = getpass(prompt).strip()
    else:
        value = input(prompt).strip()
    if value.casefold() == "back":
        raise Back
    if value.casefold() == "exit":
        raise Exit
    if any(c in value for c in "\r\n\0"):
        raise ValueError("Use a single line")
    return value


def choose(prompt: str, options: list[str], default: int | None = None) -> int:
    print(prompt)
    for index, text in enumerate(options, 1):
        print(f"  {index}. {text}")
    while True:
        raw = answer(f"Choose a number{f' [{default}]' if default else ''}: ")
        if not raw and default:
            return default
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        print("Enter one of the numbers shown, Back, or Exit.")


def secret_value(label: str, existing: str | None) -> str:
    while True:
        value = answer(label + (" (Enter keeps the saved secret)" if existing else "") + ": ", secret=True)
        if value or existing:
            return value or existing or ""
        print("A value is required. You can type Exit and resume later.")


def persist(settings: Settings, changes: dict[str, str]) -> Settings:
    # Environment overrides would make an apparently successful wizard save misleading.
    if any(key in os.environ for key in changes):
        raise ConfigurationError("Environment overrides are active")
    save_settings(settings.secrets_file, changes)
    return load_settings()


def outlook_step(settings: Settings) -> Settings:
    print("\n1. Windows and your mailbox")
    print("Use Windows 10 or 11 and Outlook (classic). Classic Outlook has a File tab.")
    print("New Outlook cannot connect. Open Outlook (classic) from Start; if missing, ask IT to install it.")
    print("Sign in and open the intended Inbox. If Outlook blocks access, ask IT; do not weaken its security settings.")
    if not windows_supported():
        raise RuntimeError("Unsupported Windows")
    profile = answer("Outlook profile name (Enter keeps saved/default profile; '-' uses the default): ")
    profile = None if profile == "-" else profile or settings.outlook_profile
    print("Checking classic Outlook; no email items will be read...")
    with OutlookClient(profile, timezone=settings.timezone) as client:
        metadata = client.profile_metadata()
    print(f"Connected profile: {metadata['profile']}\nInbox mailbox: {metadata['mailbox']}")
    if profile and metadata["profile"].casefold() != profile.casefold():
        raise RuntimeError("Wrong connected Outlook profile")
    if choose("Is this the intended profile and Inbox?", ["Yes, use this mailbox", "No, go back"], 2) == 2:
        print("Close Outlook and reopen it with the intended profile, then retry. No mailbox was authorized.")
        raise Back
    return persist(settings, {"OUTLOOK_PROFILE": profile or ""})


def soclaas_step(settings: Settings) -> Settings:
    print("\n2. Connect SoCLaaS")
    print("An API key is a private credential giving this app access to SoCLaaS models.")
    print("SoCLaaS serves the NUS School of Computing community: https://soclaas.comp.nus.edu.sg/")
    print("Use Get My API Key on that site from the NUS network. If you lack access, ask your organization/service administrator.")
    print("There is no public signup promised by this application. Type Exit to return after obtaining access.")
    key = secret_value("Paste your SoCLaaS API key (hidden)", settings.soclaas_api_key)
    candidate = replace(settings, soclaas_api_key=key)
    print("Checking access and retrieving available models...")
    models = SoCLaaSClient(candidate).list_models()
    ids = sorted({str(item["id"]) for item in models if item.get("id")})
    if not ids:
        raise RuntimeError("No available models")
    print("Choose a chat model that supports structured JSON. Embedding, image and audio models are unsuitable.")
    print("If unsure, ask the SoCLaaS administrator. Availability alone does not prove suitability;")
    print("the separate First-run Check will test a fictional classification before processing is enabled.")
    if settings.model in ids and choose("Saved model is available.", ["Keep saved model", "Choose another model"], 1) == 1:
        model = settings.model
    else:
        model = ids[choose("Available models:", ids) - 1]
    return persist(settings, {"SOCLAAS_API_KEY": key, "EMAIL_TRIAGE_MODEL": model})


def mode_step(settings: Settings) -> Settings:
    print("\n3. Choose where the application runs")
    print("Local is recommended: Outlook, classification and task state stay on this Windows computer.")
    print("Telegram commands wait while this computer is unavailable. Cloud is for an existing deployment managed by an experienced owner.")
    mode = choose("Operating mode", ["Local (recommended)", "Advanced: connect an existing cloud bot"],
                  2 if settings.cloud_bot_enabled else 1)
    if mode == 1:
        if settings.cloud_bot_enabled or any((settings.relay_url, settings.relay_token, settings.relay_encryption_key)):
            print("Saved cloud configuration exists. Switching an authoritative cloud bot requires reconciliation first.")
            print("Follow Advanced Cloud recovery before using local mode. Setup will not take ownership from a deployment.")
            raise RuntimeError("Existing cloud configuration needs owner review")
        return persist(settings, {"OUTLOOK_TRIAGE_CLOUD_BOT": "false"})
    print("Ask the deployment owner for its HTTPS URL, relay bearer token, encryption key and webhook secret.")
    print("This wizard creates no Cloudflare resources and never changes Telegram webhooks or uploads snapshots.")
    url = answer("Existing cloud HTTPS address (Enter keeps saved address): ") or settings.relay_url or ""
    token = secret_value("Relay bearer token (hidden)", settings.relay_token)
    key = secret_value("Relay encryption key (hidden)", settings.relay_encryption_key)
    secret = secret_value("Telegram webhook secret (hidden)", settings.telegram_webhook_secret)
    candidate = replace(settings, relay_url=url.rstrip("/"), relay_token=token, relay_encryption_key=key,
                        telegram_webhook_secret=secret, cloud_bot_enabled=True)
    print("Checking existing deployment (no mailbox data sent)...")
    check_cloud(candidate)
    print("The deployment owner must also confirm this bot, private chat and local database belong to that deployment.")
    print("Connectivity cannot establish dataset ownership; never point a new database at someone else's cloud state.")
    if choose("Has the owner confirmed those details?", ["Yes", "No, return later"], 2) == 2:
        raise Back
    return persist(settings, {"OUTLOOK_TRIAGE_RELAY_URL": candidate.relay_url or "",
        "OUTLOOK_TRIAGE_RELAY_TOKEN": token, "OUTLOOK_TRIAGE_RELAY_ENCRYPTION_KEY": key,
        "TELEGRAM_WEBHOOK_SECRET": secret, "OUTLOOK_TRIAGE_CLOUD_BOT": "true"})


def telegram_step(settings: Settings) -> Settings:
    print("\n4. Connect your own Telegram bot")
    print("In Telegram open https://t.me/BotFather (official @BotFather, verified account).")
    print("Send /newbot, follow BotFather's name and username instructions, and copy its token.")
    print("Use a dedicated bot for this app. Keep the token private; anyone with it can control the bot.")
    token = secret_value("Paste bot token (hidden)", settings.telegram_bot_token)
    telegram = TelegramClient(token, timeout=settings.telegram_timeout_seconds)
    print("Checking bot identity...")
    identity = telegram.identity()
    print(f"Bot: @{identity.get('username', '(username unavailable)')}")
    if choose("Is this your intended bot?", ["Yes", "No, go back"], 2) != 1:
        raise Back
    candidate = replace(settings, telegram_bot_token=token)
    print("Checking webhook compatibility; existing webhooks are never removed...")
    check_webhook(candidate, telegram)
    chat = None
    if token == settings.telegram_bot_token and settings.telegram_chat_id:
        telegram.chat_id = settings.telegram_chat_id
        try:
            chat = telegram.private_chat()
        except TelegramError:
            print("The saved private chat could not be checked. Open/unblock the bot, or choose a different private chat below.")
        if chat:
            print(f"Saved private chat: {chat.display_name}")
            if choose("Confirm the destination", ["Keep this private chat", "Choose another private chat"], 2) == 2:
                chat = None
    if chat is None and settings.cloud_bot_enabled:
        print("Cloud webhooks prevent safe chat discovery. Ask the owner for the existing private chat ID.")
        telegram.chat_id = secret_value("Existing private chat ID (hidden)", settings.telegram_chat_id)
        chat = telegram.private_chat()
        print(f"Existing private chat: {chat.display_name}")
        if choose("Confirm this is your intended private chat", ["Yes", "No"], 2) != 1:
            raise Back
    while chat is None:
        print(f"Open @{identity.get('username', 'your bot')} in Telegram and send /start in a private chat.")
        print("Take as long as you need. Stop any other local receiver first. Discovery does not acknowledge or discard updates.")
        choose("When ready", ["Find private chats"], 1)
        from .locking import process_lock
        with process_lock(settings.state_dir / "telegram-receiver.lock", stale_after_seconds=120,
                          label="Telegram receiver"):
            chats = telegram.recent_private_chats()
        if not chats:
            print("No recent private chats found. Check you opened the correct bot and sent /start.")
            choose("Next action", ["Retry after sending /start"], 1)
            continue
        selected = chats[choose("Select your private chat:", [c.display_name for c in chats]) - 1]
        print(f"Selected: {selected.display_name}")
        if choose("Authorize only this private chat?", ["Yes, authorize this chat", "No, choose again"], 2) == 1:
            chat = selected
    settings = persist(settings, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat.chat_id})
    if choose("Send the existing harmless test message? No email content is included.", ["Send test", "Skip"], 2) == 1:
        telegram.chat_id = chat.chat_id
        telegram.send_message("Outlook Triage Telegram delivery is configured successfully.")
        print("Telegram test message sent. Check your private chat. If missing, open Troubleshooting.")
    return settings


def fingerprint(settings: Settings) -> str:
    values = [settings.soclaas_api_key, settings.soclaas_base_url, settings.model, settings.outlook_profile,
              settings.telegram_bot_token, settings.telegram_chat_id, settings.cloud_bot_enabled,
              settings.relay_url, settings.relay_token, settings.relay_encryption_key, settings.telegram_webhook_secret]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def verified(settings: Settings) -> bool:
    try:
        return (settings.state_dir / "first-run-verified").read_text(encoding="ascii") == fingerprint(settings)
    except OSError:
        return False


def offer_background(settings: Settings) -> None:
    print("\nBackground operation reads eligible Inbox emails and sends cleaned sender, subject, date and body text to SoCLaaS.")
    print("Digests send summaries, tasks, sender details and deadlines to Telegram. /show sends bounded original body text.")
    print("Cloud mode also sends encrypted snapshots to your deployment. Original email preview caching remains OFF.")
    print("Automatic runs do not mark emails read. Authorized /read, /done and /dismiss commands DO mark originals read.")
    print("Only enable processing if your organization permits these transfers.")
    print("Local schedules: sync 03:50/07:50/11:50/15:50/19:50/23:50; digest 08:00, in Windows local time.")
    print(f"Report dates/deadlines use {settings.timezone_name}. Cloud schedules use Asia/Singapore.")
    print("You must stay signed in. No wake from sleep. Missed timed runs catch up when available; queued commands can run on resume.")
    if choose("Enable regular processing now? Missed scheduled runs may start immediately.",
              ["Enable background operation", "Keep background operation off"], 2) == 1:
        checks = collect(settings, schedules=False)
        if any(c.status == "FAIL" for c in checks) or not verified(settings):
            display(checks)
            print("Background operation was not enabled. Complete First-run Check and retry.")
            return
        plan = scheduler.task_plan(cloud=settings.cloud_bot_enabled)
        scheduler.register_tasks(plan)
        scheduler.start_listeners(plan)
        print("Background operation enabled. Send /status to your bot now; it should reply with sync/task status.")
        print("Send /list only when ready to process eligible email and send a digest. Open Troubleshooting if no reply.")
    else:
        print("Background operation remains off. Reopen First-run Check when ready to enable it.")


def setup() -> int:
    print("Outlook Triage Setup\nType Back to revisit a step, Exit to leave, or press Ctrl+C. Valid saved settings are retained.")
    print("Setup reads profile metadata and checks connections. It does not classify mailbox messages, mark email read or change task states.")
    print("Before processing: cleaned email text goes to SoCLaaS; summaries and requested previews go to Telegram.")
    print("You need classic Outlook signed in, SoCLaaS access, a Telegram account and permission from your organization.")
    step = 0
    steps = [("outlook", outlook_step), ("soclaas", soclaas_step), ("cloud", mode_step), ("telegram", telegram_step)]
    try:
        settings = load_settings()
        if not windows_supported():
            display([Check("windows", "FAIL")])
            return 1
        # Do not silently change credentials or mode under a running installation.
        plan = scheduler.task_plan(cloud=False)
        plan["tasks"] += scheduler.task_plan(cloud=True)["tasks"]
        active = any(row["registered"] and row["enabled"] for row in scheduler.inspect_tasks(plan))
        if active:
            if choose("Background tasks exist for this installation. Stop them before reviewing settings?",
                      ["Stop this installation's background tasks", "Exit without changes"], 2) == 2:
                raise Exit
            scheduler.remove_tasks(plan)
        from .cli import initialize
        initialize(settings)
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        while step < len(steps):
            name, action = steps[step]
            try:
                settings = action(settings)
                step += 1
            except Back:
                step = max(0, step - 1)
            except Exit:
                raise
            except Exception as exc:
                display([Check("configuration" if isinstance(exc, ConfigurationError) else name,
                               "FAIL", safe_failure(exc))])
                if isinstance(exc, ConfigurationError):
                    print("Some settings may have been supplied outside Setup. Ask whoever configured them to remove conflicting overrides, then retry.")
                next_action = choose("What next?", ["Retry this step", "Back", "Exit"], 1)
                if next_action == 2:
                    step = max(0, step - 1)
                elif next_action == 3:
                    raise Exit
        print("\n5. Final connection checks...")
        checks = collect(settings, schedules=False)
        display(checks)
        if any(c.status == "FAIL" for c in checks):
            print("Setup is incomplete. Settings saved; background operation is off. Open Troubleshooting, then reopen Setup.")
            return 1
        print("\nSetup connections complete. No mailbox processing was started.")
        print("NEXT: close this window and open Outlook Triage - First-run Check from Start.")
        print("It tests fictional text and asks you to confirm a Telegram test before offering background operation.")
        print(f"Settings: {settings.config_dir}\nReports: {settings.reports_dir}\nLogs: {settings.log_file.parent}")
        return 0
    except (Exit, Back, EOFError, KeyboardInterrupt):
        print(f"\nSetup stopped at step {step + 1}. Completed settings were retained. Reopen Setup to resume; review each step with saved values.")
        print("Background operation has not been enabled by this run. Next: reopen Setup from Start.")
        return 130
    except Exception as exc:
        print(f"Setup could not continue ({safe_failure(exc)}). Open Troubleshooting, resolve the reported problem, then reopen Setup.")
        return 1


def verify() -> int:
    print("First-run Check - separate from Setup. No mailbox messages will be read or classified.")
    try:
        settings = load_settings()
        checks = collect(settings, schedules=False)
        display(checks)
        if any(c.status == "FAIL" for c in checks):
            print("Complete Setup and resolve failed checks before continuing. Background operation was not enabled.")
            return 1
        print("This test sends only fictional sender, subject, date and text to your chosen SoCLaaS model.")
        print("It then sends the harmless Telegram test. This verifies structured output and delivery without your email.")
        if choose("Run the separate first-run test?", ["Run fictional test", "Exit"], 2) == 2:
            raise Exit
        from .models import EmailForClassification
        print("Testing the selected model with fictional text...")
        SoCLaaSClient(settings).classify(EmailForClassification(
            source_id="first-run-fictional", sender="Example Sender <example@example.invalid>",
            subject="Fictional setup check", received_at=datetime.now(settings.timezone).isoformat(),
            body="This is a fictional installation check. Please reply to confirm tomorrow's meeting."),
            datetime.now(settings.timezone))
        telegram = TelegramClient(settings.telegram_bot_token or "", settings.telegram_chat_id,
                                  timeout=settings.telegram_timeout_seconds)
        telegram.send_message("Outlook Triage Telegram delivery is configured successfully.")
        if choose("Did that test arrive in your intended private chat?", ["Yes, I received it", "No"], 2) == 2:
            print("First-run Check is incomplete. Open Troubleshooting; background operation was not enabled.")
            return 1
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        (settings.state_dir / "first-run-verified").write_text(fingerprint(settings), encoding="ascii")
        print("First-run Check passed: connections, fictional classification and confirmed Telegram delivery.")
        offer_background(settings)
        return 0
    except (Back, Exit, EOFError, KeyboardInterrupt):
        print("First-run Check stopped. Reopen First-run Check when ready.")
        return 130
    except Exception as exc:
        print(f"First-run Check could not finish ({safe_failure(exc)}). Open Troubleshooting; retry First-run Check after recovery.")
        return 1
