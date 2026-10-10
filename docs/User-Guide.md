# Outlook–SoCLaaS Triage User Guide

This guide is the reference for operating Outlook–SoCLaaS Triage after installation. Complete the installation, configuration, and manual safety checks in the [README](../README.md) before using these commands or registering scheduled tasks.

The supported user interfaces are the `outlook-triage` command-line program and `scripts\register_windows_tasks.ps1`. The application does not expose a public HTTP API or a stable Python SDK. Python modules and other scripts in the repository are internal implementation details.

## Command basics

Run commands from PowerShell after activating the project virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
outlook-triage --help
```

Use `--help` after a command to see its syntax. `--verbose` is a global option and must appear before the command; it enables detailed console logging in addition to the rotating application log.

```powershell
outlook-triage sync --help
outlook-triage --verbose sync
```

Unless a command says otherwise, it loads configuration from `%APPDATA%\OutlookTriage\secrets.env`, uses the signed-in classic Outlook profile, and writes application state under `%LOCALAPPDATA%\OutlookTriage`.

## Recommended workflow

After the README safety checks, run two manual synchronizations and inspect the resulting digest before enabling a schedule:

```powershell
outlook-triage sync
outlook-triage sync
outlook-triage digest
outlook-triage tasks list
```

The first synchronization scans the configured bootstrap period. Later runs rescan an overlap window and deduplicate messages, so repeating `sync` is safe. Resolve any failed classifications with `retry-failed`, manage generated tasks locally, and generate a fresh digest as needed.

## CLI reference

### `init`

```powershell
outlook-triage init
```

Creates the configuration templates and runtime directories and initializes the SQLite database. Existing configuration files are never overwritten. It prints the files it created and the database location.

Run this once after installation. It is also safe to run again to create any missing template or directory.

### `models`

```powershell
outlook-triage models
```

Calls the configured SoCLaaS service and prints the model IDs available to `SOCLAAS_API_KEY`, including a context size when the service provides one. Copy the exact chosen ID into `EMAIL_TRIAGE_MODEL`.

This command requires `SOCLAAS_API_KEY` and a reachable `SOCLAAS_BASE_URL`; it does not require `EMAIL_TRIAGE_MODEL` to be set.

### `check-mail`

```powershell
outlook-triage check-mail
outlook-triage check-mail --limit 25
```

Prints the received time, sender, subject, and Outlook `EntryID` for the newest messages. The default limit is 10.

This is a metadata-only safety check: it does not read message bodies, call SoCLaaS, apply synchronization eligibility rules, or write message records to the database. It requires classic Outlook and the selected profile to be available.

### `classify-one`

```powershell
outlook-triage classify-one "OUTLOOK_ENTRY_ID"
```

Fetches the selected Outlook item, cleans and truncates its body, verifies the configured model, and asks SoCLaaS to classify it. The resulting structured JSON includes action status, urgency, action type, task text, deadline information, summary, category, reason, and confidence.

The classification is printed but is not saved. This command may read sensitive message content and send the cleaned content to SoCLaaS. Use a non-sensitive message for initial validation, and run `sync` when results should be persisted.

### `sync`

```powershell
outlook-triage sync
```

Scans classic Outlook, stores message metadata, applies explicit filters, classifies eligible messages with SoCLaaS, and creates local tasks for messages requiring action. It prints counts in this form:

```text
processed=3 skipped=1 failed=0 unchanged=4
```

On the first run, the scan begins `OUTLOOK_TRIAGE_BOOTSTRAP_DAYS` before the current time. Later runs begin before the stored high-water timestamp by `OUTLOOK_TRIAGE_OVERLAP_HOURS`. Messages are deduplicated by Outlook store ID and `EntryID`.

Within the scan window, only unread messages or messages with an active Outlook follow-up flag are eligible. Read messages without an active flag, including completed flags, are skipped without reading their bodies. Pinned messages are not included unless they are unread or actively flagged because the documented classic Outlook object model does not expose pin state.

The command does not change any Outlook item. Metadata and the high-water timestamp are committed before classification, so an interrupted or partially failed classification run does not lose discovered messages. A process lock prevents overlapping synchronization and retry runs.

### `retry-failed`

```powershell
outlook-triage retry-failed
outlook-triage retry-failed --limit 100
```

Retries locally pending or failed messages, oldest first, by refetching them from Outlook and applying the current filters and SoCLaaS configuration. The default limit is 50. It prints the same count fields as `sync` and shares the synchronization process lock.

Use this after correcting connectivity, credentials, model selection, or invalid model output. A message that no longer exists or remains inaccessible stays failed for a later retry.

### `digest`

```powershell
outlook-triage digest
outlook-triage digest --telegram
```

Without `--telegram`, builds the current deterministic Markdown digest from the local database, prints it, and saves it as `%LOCALAPPDATA%\OutlookTriage\reports\YYYY-MM-DD.md`. It never sends the digest externally.

The digest shows synchronization health, pending classification warnings, overdue tasks, urgent or due-today tasks, tasks due within `OUTLOOK_TRIAGE_DIGEST_HORIZON_DAYS`, other open actions, waiting items, and 24-hour category counts. Running it again on the same day replaces that day's local report with a current snapshot.

With `--telegram`, the command saves the local report, freezes formatted Telegram messages in the SQLite outbox, and then attempts delivery. If an active delivery for the same date already exists, it resumes that frozen delivery instead of regenerating the report. See [Telegram delivery](#telegram-delivery) for recovery and privacy behavior.

### `tasks`

Task commands change only the local SQLite database. They never update flags, categories, or other state in Outlook.

List all tasks or filter by one supported status:

```powershell
outlook-triage tasks list
outlook-triage tasks list --status open
outlook-triage tasks list --status waiting
outlook-triage tasks list --status done
outlook-triage tasks list --status dismissed
```

Each row shows its numeric ID, status, urgency, description, and deadline. Use that numeric ID with an update command:

| Command | Resulting status | Example |
| --- | --- | --- |
| `tasks done TASK_ID` | `done` | `outlook-triage tasks done 12` |
| `tasks waiting TASK_ID` | `waiting` | `outlook-triage tasks waiting 15` |
| `tasks dismiss TASK_ID` | `dismissed` | `outlook-triage tasks dismiss 18` |
| `tasks reopen TASK_ID` | `open` | `outlook-triage tasks reopen 12` |

An unknown task ID reports an error and makes no change. A later digest reflects the current local status.

### `telegram-chats`

```powershell
outlook-triage telegram-chats
```

Lists recent private chats visible to the configured bot as a chat ID and display name. It ignores groups and requires `TELEGRAM_BOT_TOKEN`, but not `TELEGRAM_CHAT_ID`.

First open the bot's private chat in Telegram and send `/start`. Then run this command and place the intended numeric ID in `TELEGRAM_CHAT_ID`. If there are no recent private chats, the command explains how to start one and exits unsuccessfully.

### `telegram-test`

```powershell
outlook-triage telegram-test
```

Sends a fixed, harmless configuration-success message to the configured private chat. It requires both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; it does not include Outlook or task data.

### `telegram-retry`

```powershell
outlook-triage telegram-retry
```

Reactivates queued deliveries that were paused for attention and tries the Telegram outbox again. Use it only after correcting the reported credential, chat, access, or content problem. The summary reports how many deliveries were reactivated, completed, still need attention, or expired.

The command resumes stored message chunks; it does not regenerate their Markdown reports. Queued deliveries are processed oldest first.

### `telegram-poll`

```powershell
outlook-triage telegram-poll
```

Checks once for inbound commands, processes them in update order, prints the number completed, and exits. Use this for setup and diagnostics. It requires both Telegram settings and refuses to run while a Telegram webhook is configured or another receiver is active.

### `telegram-listen`

```powershell
outlook-triage telegram-listen
```

Runs a foreground long-polling listener until interrupted. It uses outbound HTTPS only and does not expose a local HTTP server. Transient Telegram failures use bounded backoff. Run only one listener; a receiver lock prevents this command, `telegram-poll`, and `telegram-chats` from consuming updates concurrently.

The configured private chat supports:

| Command | Behavior |
| --- | --- |
| `/read #42` | Marks the task's original Outlook email read. The local task status is unchanged. Repeating the command is safe. |
| `/show #42` | Sends a bounded, escaped plain-text body preview without changing Outlook read state. Attachment contents are never read. |
| `/help` or `/start` | Shows the available commands. |

Task IDs are the numeric IDs shown in the digest and `tasks list`. Commands from other chats and bot-authored messages are ignored. The listener briefly acquires the synchronization lock for Outlook access, so synchronization and Telegram commands cannot use Outlook concurrently.

## Telegram delivery

Telegram is optional. To enable it:

1. Use the verified **@BotFather** in Telegram, create a bot with `/newbot`, and keep the token private.
2. Open a private chat with the bot and send `/start`.
3. Set `TELEGRAM_BOT_TOKEN` in `%APPDATA%\OutlookTriage\secrets.env`.
4. Run `outlook-triage telegram-chats` and set the intended `TELEGRAM_CHAT_ID`.
5. Run `outlook-triage telegram-test`.
6. Send a real digest with `outlook-triage digest --telegram`.

The delivered digest begins with a summary and sends non-empty task sections as compact checklists. Long content is HTML-escaped and split into ordered messages. Confirmed chunks are recorded and are not deliberately resent. Telegram has no request idempotency key, so a chunk may appear twice if Telegram accepted it but the network failed before acknowledging it.

Transient network, rate-limit, and Telegram server failures leave the delivery pending and return an error so a Telegram-enabled scheduled task can retry. A resumed delivery starts with a delayed-delivery notice. Invalid credentials, an inaccessible or blocked chat, and permanent content rejection pause affected deliveries and request user attention instead of repeatedly retrying. On Windows, the application attempts to show a tray warning. Correct the problem and run `outlook-triage telegram-retry`.

Queued messages are retained in SQLite for up to seven days and then expire; the local Markdown report remains available. The bot token, Telegram response bodies, and digest contents are not written to application logs.

Telegram digest delivery receives summaries, task descriptions, sender information, and deadlines. The `/show` command additionally transmits up to `TELEGRAM_BODY_PREVIEW_CHARS` characters of the original plain-text body. Do not enable these features unless your organization permits that information to be sent to Telegram.

## Windows scheduling

Register schedules only after the manual checks and two consecutive `sync` runs succeed. From the project directory, preview the exact plan without reading or changing Task Scheduler:

```powershell
& .\scripts\register_windows_tasks.ps1 -ShowPlan
```

Register local-only digest generation:

```powershell
& .\scripts\register_windows_tasks.ps1
```

Or register the daily digest with Telegram delivery and transient-failure retries:

```powershell
& .\scripts\register_windows_tasks.ps1 -EnableTelegram
```

Add the inbound command listener at user logon independently or together with digest delivery:

```powershell
& .\scripts\register_windows_tasks.ps1 -EnableTelegramCommands
& .\scripts\register_windows_tasks.ps1 -EnableTelegram -EnableTelegramCommands
```

The script supports these parameters:

| Parameter | Behavior |
| --- | --- |
| `-ProjectDir PATH` | Uses another project directory. The default is the parent of the script directory. The project must contain `.venv\Scripts\outlook-triage.exe` when registering tasks. |
| `-TaskPrefix TEXT` | Changes the scheduled-task name prefix. The default creates `Outlook SoCLaaS Triage - Sync` and `Outlook SoCLaaS Triage - Digest`. |
| `-EnableTelegram` | Runs `digest --telegram` and gives the digest task up to 47 retries at 30-minute intervals when it exits unsuccessfully. |
| `-EnableTelegramCommands` | Adds an at-logon `telegram-listen` task with restart-on-failure behavior. |
| `-ShowPlan` | Prints the complete task plan as JSON and returns without accessing or modifying Task Scheduler. |

For example, preview a custom project and task prefix:

```powershell
& .\scripts\register_windows_tasks.ps1 `
    -ProjectDir "C:\Tools\outlook-soclaas-triage" `
    -TaskPrefix "My Outlook Triage" `
    -EnableTelegram `
    -ShowPlan
```

Registration creates or replaces the two base tasks for the current Windows user and, when enabled, the Telegram Commands task:

- Synchronization at 03:50, 07:50, 11:50, 15:50, 19:50, and 23:50.
- Digest generation daily at 08:00.
- Telegram command listening at user logon, with one-minute failure restarts and no execution-time limit.

The tasks run with limited privileges only while that user is logged in. They start after a missed scheduled time when the user becomes available, can run on battery power, do not wake the computer, and ignore a new trigger while an earlier instance is still running. Sync and digest runs stop after two hours; the listener has no execution-time limit. Re-run registration after moving the project, changing the desired prefix or Telegram mode, or upgrading an older task definition.

## Configuration and local data

Values in the process environment override values read from `secrets.env`. Blank values are treated as unset.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SOCLAAS_API_KEY` | unset | SoCLaaS credential. |
| `SOCLAAS_BASE_URL` | `https://soclaas-api.comp.nus.edu.sg/v1` | OpenAI-compatible SoCLaaS base URL. |
| `EMAIL_TRIAGE_MODEL` | unset | Exact model ID used for classification. |
| `OUTLOOK_PROFILE` | default profile | Optional classic Outlook profile name. |
| `OUTLOOK_TRIAGE_TIMEZONE` | `Asia/Singapore` | IANA timezone used for dates, deadlines, and reports. |
| `OUTLOOK_TRIAGE_BOOTSTRAP_DAYS` | `7` | Initial synchronization lookback. |
| `OUTLOOK_TRIAGE_OVERLAP_HOURS` | `8` | Rescan overlap before the synchronization high-water mark. |
| `OUTLOOK_TRIAGE_MAX_BODY_CHARS` | `12000` | Maximum cleaned body characters sent for classification. |
| `OUTLOOK_TRIAGE_DIGEST_HORIZON_DAYS` | `7` | Upcoming-deadline window in the digest. |
| `OUTLOOK_TRIAGE_TIMEOUT_SECONDS` | `45` | SoCLaaS request timeout. |
| `TELEGRAM_BOT_TOKEN` | unset | Optional Telegram bot credential. |
| `TELEGRAM_CHAT_ID` | unset | Optional private destination chat ID. |
| `TELEGRAM_TIMEOUT_SECONDS` | `20` | Telegram request timeout. |
| `TELEGRAM_BODY_PREVIEW_CHARS` | `6000` | Maximum original body characters returned by `/show`. |

Advanced path overrides are available as process environment variables:

| Setting | Default location |
| --- | --- |
| `OUTLOOK_TRIAGE_CONFIG_DIR` | `%APPDATA%\OutlookTriage` |
| `OUTLOOK_TRIAGE_DATA_DIR` | `%LOCALAPPDATA%\OutlookTriage` |
| `OUTLOOK_TRIAGE_STATE_DIR` | `%LOCALAPPDATA%\OutlookTriage\state` |
| `OUTLOOK_TRIAGE_SECRETS_FILE` | `%APPDATA%\OutlookTriage\secrets.env` |
| `OUTLOOK_TRIAGE_RULES_FILE` | `%APPDATA%\OutlookTriage\rules.yaml` |
| `OUTLOOK_TRIAGE_DATABASE` | `%LOCALAPPDATA%\OutlookTriage\outlook-triage.db` |
| `OUTLOOK_TRIAGE_REPORTS_DIR` | `%LOCALAPPDATA%\OutlookTriage\reports` |
| `OUTLOOK_TRIAGE_LOG_FILE` | `%LOCALAPPDATA%\OutlookTriage\logs\outlook-triage.log` |
| `OUTLOOK_TRIAGE_LOCK_DIR` | `%LOCALAPPDATA%\OutlookTriage\state\sync.lock` |

The SQLite database contains discovered message metadata, classifications, local tasks, run history, pending Telegram chunks, the inbound update offset, and a minimal command audit. The audit stores command type, task ID, state, authorized chat ID, and sanitized error category; it does not store raw command text, email bodies, or Outlook identifiers. Reports are Markdown files. Logs rotate and exclude message bodies, API keys, and full Outlook `EntryID` values.

## Filtering behavior

Edit `%APPDATA%\OutlookTriage\rules.yaml` to define exact sender addresses, exact sender domains, and regular expressions matched against subjects:

```yaml
ignored_senders:
  - newsletter@example.com
ignored_domains:
  - marketing.example.com
ignored_subject_patterns:
  - '^Weekly digest'
```

All matches are case-insensitive. Filters are applied before a message body is sent to SoCLaaS, and a match is recorded as explicitly skipped. The application does not implicitly discard all `no-reply` senders or low-importance messages.

Before classification, the application removes common quoted-message history, quoted lines, URL query strings and fragments, excess whitespace, and text beyond `OUTLOOK_TRIAGE_MAX_BODY_CHARS`.

## Exit status and troubleshooting

| Exit code | Meaning |
| --- | --- |
| `0` | The command completed; a Telegram summary may still report deliveries paused for user attention. |
| `1` | Configuration, locking, Outlook, SoCLaaS, Telegram, or requested-record failure. |
| `2` | `sync` or `retry-failed` completed with one or more failed classifications; invalid command syntax also uses the standard parser exit code 2. |
| `130` | The user interrupted the command with Ctrl+C. |

Common failures behave as follows:

- **Classic Outlook is missing or unavailable:** the command stops without changing Outlook. Confirm classic Outlook is installed, open, configured, and signed in.
- **Profile or Inbox is unavailable:** check `OUTLOOK_PROFILE`; leave it blank to use the default profile.
- **Outlook Programmatic Access is denied:** do not weaken Trust Center, antivirus, registry, or Group Policy settings. Ask the organization administrator to resolve the policy restriction.
- **Outlook is busy or COM access fails:** the run fails and preserves the previous synchronization watermark. Close blocking dialogs, confirm Outlook is responsive, and retry.
- **Another sync or retry is running:** wait for it to finish. Investigate a stale lock only after confirming no `outlook-triage` process is active.
- **SoCLaaS returns `429`, times out, cannot connect, or returns `5xx`:** classification retries with exponential backoff and jitter. Remaining failures are retained for `retry-failed`.
- **SoCLaaS returns `401` or `403`:** correct the API key or access policy; the request is not retried as a transient failure.
- **The model returns invalid JSON:** the application attempts one repair request, then retains the message for `retry-failed`.
- **A digest warns that no sync completed or the latest sync failed:** resolve the synchronization problem, run `sync` or `retry-failed`, and generate the digest again.
- **Telegram delivery needs attention:** inspect the sanitized local log, correct the configuration or chat problem, and run `outlook-triage telegram-retry`.
- **Telegram listener reports a webhook conflict:** clear the bot's webhook before using long polling; Telegram does not permit both receivers simultaneously.
- **Telegram listener says another receiver is active:** stop the other listener or wait for `telegram-poll`/`telegram-chats` to finish. Investigate the receiver lock only after confirming no listener process is active.
- **A Telegram Outlook command fails:** confirm classic Outlook is open and responsive under the scheduled task's interactive Windows user, then retry the command.

Use the global verbose option for interactive diagnosis:

```powershell
outlook-triage --verbose sync
```

Do not post `secrets.env`, the SQLite database, reports, or verbose output containing organizational details in public support channels.
