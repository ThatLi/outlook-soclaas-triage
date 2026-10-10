# Outlook–SoCLaaS Triage for Classic Outlook

A local Windows application that reads the signed-in **classic Outlook** profile, filters selected messages, uses SoCLaaS for structured classification, stores metadata and tasks in SQLite, and produces a deterministic Markdown digest. Its scheduled synchronization remains read-only; an explicitly enabled Telegram command listener provides the daily sync, digest, retry, task-list, and task-state workflows.

It does not require Microsoft Entra app registration. It never sends, moves, flags, or deletes Outlook messages, and it never reads attachment contents. The only supported Outlook mutation is marking email read through an authorized `/read`, `/done`, or `/dismiss` command from the configured private Telegram chat; `/waiting` and `/reopen` change local workflow state only. Confirm that sending cleaned email text to SoCLaaS—and digest or optional `/show` content to Telegram—is permitted by your organization.

For daily operation, the complete command reference, scheduling, Telegram delivery, and troubleshooting, see the [User Guide](docs/User-Guide.md).

## Requirements

- Windows 10 or 11.
- Python 3.11 or newer.
- Classic Outlook installed, configured, and signed in to the intended mailbox.
- An active Windows login during scheduled runs.

New Outlook does not provide the Outlook Object Model used by this application. If necessary, switch back to classic Outlook before continuing.

## 1. Install natively on Windows

Open PowerShell in this project directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
outlook-triage init
```

`init` creates, without overwriting existing files:

- `%APPDATA%\OutlookTriage\secrets.env`
- `%APPDATA%\OutlookTriage\rules.yaml`
- `%LOCALAPPDATA%\OutlookTriage\outlook-triage.db`
- `%LOCALAPPDATA%\OutlookTriage\reports\`
- `%LOCALAPPDATA%\OutlookTriage\logs\`

## 2. Configure SoCLaaS and Outlook

Edit `%APPDATA%\OutlookTriage\secrets.env`:

```dotenv
SOCLAAS_API_KEY=your-api-key
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_TIMEOUT_SECONDS=20
TELEGRAM_BODY_PREVIEW_CHARS=6000
SOCLAAS_BASE_URL=https://soclaas-api.comp.nus.edu.sg/v1
EMAIL_TRIAGE_MODEL=
OUTLOOK_PROFILE=
OUTLOOK_TRIAGE_TIMEZONE=Asia/Singapore
OUTLOOK_TRIAGE_BOOTSTRAP_DAYS=7
OUTLOOK_TRIAGE_OVERLAP_HOURS=8
OUTLOOK_TRIAGE_MAX_BODY_CHARS=12000
OUTLOOK_TRIAGE_DIGEST_HORIZON_DAYS=7
OUTLOOK_TRIAGE_TIMEOUT_SECONDS=45
```

Leave `OUTLOOK_PROFILE` blank to use the default profile. Set it only when classic Outlook has multiple named profiles and the default is not the intended one.

List available SoCLaaS models and set the exact selected ID in `EMAIL_TRIAGE_MODEL`:

```powershell
outlook-triage models
```

## 3. Perform manual safety checks

Open classic Outlook and confirm the intended Inbox. Then retrieve metadata only:

```powershell
outlook-triage check-mail --limit 10
```

This command does not access message bodies or call SoCLaaS. Select a non-sensitive `EntryID` from the output and test classification without saving it:

```powershell
outlook-triage classify-one "OUTLOOK_ENTRY_ID"
```

Outlook may display a Programmatic Access warning depending on organizational policy and endpoint-security status. Do not weaken Trust Center, antivirus, registry, or Group Policy settings to bypass it. If access is denied, consult the organization administrator.

## 4. Configure explicit filters

Edit `%APPDATA%\OutlookTriage\rules.yaml`:

```yaml
ignored_senders:
  - newsletter@example.com
ignored_domains:
  - marketing.example.com
ignored_subject_patterns:
  - '^Weekly digest'
```

Matches are case-insensitive and subject patterns are regular expressions. The application deliberately does not discard all `no-reply` or low-importance messages.

Continue with the [User Guide](docs/User-Guide.md) for synchronization, task management, digests, Telegram delivery, and Windows scheduling.

## Test

```powershell
python -m pytest
```

All Outlook tests use mock COM objects and do not open or access a live mailbox.
