# Outlook–SoCLaaS Triage for Classic Outlook

A local, read-only Windows application that reads the signed-in **classic Outlook** profile, filters selected messages, uses SoCLaaS for structured classification, stores metadata and tasks in SQLite, and produces a deterministic Markdown digest.

It does not require Microsoft Entra app registration. It never sends, moves, flags, deletes, or modifies Outlook messages, and it never reads attachment contents. Confirm that sending cleaned email text to SoCLaaS is permitted by your organization and acceptable under SoCLaaS's retention and logging policies.

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

The Windows database has a new filename and does not modify the old WSL/Graph database.

## 2. Configure SoCLaaS and Outlook

Edit `%APPDATA%\OutlookTriage\secrets.env`:

```dotenv
SOCLAAS_API_KEY=your-api-key
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SOCLAAS_BASE_URL=https://soclaas-api.comp.nus.edu.sg/v1
EMAIL_TRIAGE_MODEL=
OUTLOOK_PROFILE=
OUTLOOK_TRIAGE_TIMEZONE=Asia/Singapore
OUTLOOK_TRIAGE_BOOTSTRAP_DAYS=7
OUTLOOK_TRIAGE_OVERLAP_HOURS=8
OUTLOOK_TRIAGE_MAX_BODY_CHARS=12000
OUTLOOK_TRIAGE_DIGEST_HORIZON_DAYS=7
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

## 5. Run synchronization and task management

The first synchronization scans the previous seven days. Later runs scan from the last high-water timestamp with an eight-hour overlap and deduplicate by Outlook store ID and EntryID.

```powershell
outlook-triage sync
outlook-triage retry-failed
outlook-triage digest
```

Task commands:

```powershell
outlook-triage tasks list
outlook-triage tasks list --status waiting
outlook-triage tasks done 12
outlook-triage tasks waiting 15
outlook-triage tasks dismiss 18
outlook-triage tasks reopen 12
```

These commands update only the local SQLite database. Digest files are saved under `%LOCALAPPDATA%\OutlookTriage\reports`.

## Telegram digest delivery

Telegram delivery is optional. Digest generation always saves the local Markdown file first, and the normal `digest` command never sends it anywhere.

1. In Telegram, use the verified **@BotFather**, run `/newbot`, and keep the returned token private.
2. Open the new bot's private chat and send `/start`.
3. Put the token in `%APPDATA%\OutlookTriage\secrets.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=replace-with-real-token
   ```

4. Discover the private chat ID:

   ```powershell
   outlook-triage telegram-chats
   ```

5. Add the displayed numeric ID to the same secrets file:

   ```dotenv
   TELEGRAM_CHAT_ID=123456789
   ```

6. Send a harmless test, then send a real digest:

   ```powershell
   outlook-triage telegram-test
   outlook-triage digest --telegram
   ```

Long digests are HTML-escaped and split into ordered Telegram messages. If delivery fails, the saved Markdown report remains available locally and the command exits with an error. The bot token, Telegram response bodies, and digest contents are not written to application logs.

To enable delivery for the 08:00 scheduled digest, register the tasks with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_windows_tasks.ps1 -EnableTelegram
```

Preview the complete task definitions without reading or changing Task Scheduler:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_windows_tasks.ps1 -EnableTelegram -ShowPlan
```

Telegram receives summaries, task descriptions, sender information, and deadlines. Do not enable this feature unless sending that information to Telegram is permitted by your organization.

## 6. Register native Windows schedules

Only schedule the application after the manual checks and two consecutive synchronization runs succeed:

```powershell
& .\scripts\register_windows_tasks.ps1
```

It registers interactive tasks for the current Windows user:

- Synchronization at 03:50, then every four hours through 23:50.
- Digest generation daily at 08:00.
- Start-after-missed-run behavior and overlapping-run prevention.

If legacy tasks still invoke WSL, the script refuses to overwrite them. Inspect them first:

```powershell
& .\scripts\manage_legacy_wsl_tasks.ps1
& .\scripts\manage_legacy_wsl_tasks.ps1 -Remove -WhatIf
```

Remove only the two confirmed legacy WSL tasks:

```powershell
& .\scripts\manage_legacy_wsl_tasks.ps1 -Remove
```

Alternatively, replace them while registering the native tasks:

```powershell
& .\scripts\register_windows_tasks.ps1 -ReplaceLegacyWslTasks
```

## 7. Archive and remove the old WSL installation

The rollback utility targets only:

- `~/outlook-triage`
- `~/.config/outlook-triage`
- `~/.local/share/outlook-triage`
- `~/.local/state/outlook-triage`

Run it from a WSL terminal using the script's `/mnt/c/...` path. Review the non-mutating dry run first:

```bash
bash /mnt/c/PATH/TO/THIS/PROJECT/scripts/rollback_wsl.sh --dry-run
```

After confirming every printed target, archive and remove the installation:

```bash
bash /mnt/c/PATH/TO/THIS/PROJECT/scripts/rollback_wsl.sh --execute
```

Execution creates a private backup under:

```text
~/outlook-triage-rollback-backups/
```

The archive includes source, configuration, credentials, database, reports, logs, and the MSAL cache. Reproducible virtual environments and caches are excluded. The script verifies both the tar archive and its SHA-256 checksum before removing anything.

Because the archive contains credentials, keep it private. The script prints the exact restore command, equivalent to:

```bash
tar -xzf ~/outlook-triage-rollback-backups/outlook-triage-wsl-TIMESTAMP.tar.gz -C ~
```

Retain the backup until the Windows-native version has completed several successful scheduled runs. Delete it manually only when it is no longer required.

## Failure behavior

- Missing or new-Outlook-only installation: stop and request classic Outlook.
- Missing Outlook profile or Inbox: stop without changing Outlook.
- Object Model Guard denial: explain the policy failure and do not bypass it.
- COM-busy or unavailable Outlook: fail the run and preserve the synchronization watermark.
- SoCLaaS `429`, timeout, connection failure, or `5xx`: retry with exponential backoff and jitter.
- SoCLaaS `401` or `403`: stop with a configuration error.
- Invalid model JSON: attempt one repair, then retain the message for `retry-failed`.
- Message metadata and the high-water timestamp are committed before classification, so interrupted AI processing does not lose messages.

Logs rotate under `%LOCALAPPDATA%\OutlookTriage\logs` and do not include message bodies, API keys, or Outlook EntryIDs in full.

## Test

```powershell
python -m pytest
```

All Outlook tests use mock COM objects and do not open or access a live mailbox.

