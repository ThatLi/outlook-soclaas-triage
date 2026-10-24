# Cloud Telegram bot with a Windows Outlook bridge

This mode needs no Entra registration, Power Automate, or paid hosting. Cloudflare
receives all Telegram commands and owns task states. Windows connects outbound to
read Outlook, run the existing SoCLaaS classification, and execute queued mailbox
changes. **New mailbox data and mailbox mutations still require Windows to be on
and its Outlook user logged in.** Classification remains on the bridge in this
version; published cloud tasks work independently.

## Command behavior

| Command | With Windows off |
| --- | --- |
| `/help`, `/status`, `/tasks [status]` | Respond from cloud state immediately |
| `/waiting #id [...]`, `/reopen #id [...]` | Update cloud tasks immediately |
| `/list` | Show a dated cloud digest and queue synchronization; send a refreshed digest after completion |
| `/sync`, `/retry [1-100]` | Queue mailbox work |
| `/read`, `/done`, `/dismiss` | Queue work; confirm only after Outlook succeeds |
| `/show #id` | Show an opt-in cached preview, or explain that none is cached |

IDs remain the original SQLite task numbers. Batches validate every ID before
work starts. Each successful mailbox item is checkpointed separately; later
failure keeps earlier successes. `/read` leaves task state unchanged. `/done` and
`/dismiss` change cloud task status only after confirmation. Newer `/waiting` or
`/reopen` commands take precedence over older queued completions. Repeated
Telegram update IDs never create a second command.

### Quiet task feedback

`/done`, `/dismiss`, `/waiting`, `/reopen`, and `/read` update the latest tracked
digest containing each task. Accepted commands are deleted from the private chat
only after their feedback has been delivered. Invalid commands remain visible.
Mailbox commands show a pending label until Outlook acknowledges each item:

| Task state | Appearance |
| --- | --- |
| Open | `☐ #42 Submit report` |
| Done requested | `☐ #42 · ⏳ Done pending Submit report` |
| Done confirmed | `☑ #42 Submit report` |
| Dismissed confirmed | `☒ #42 Submit report` |
| Waiting | `☐ #42 · 💤 Waiting Submit report` |

`/read` shows read progress without completing the task. Successful task commands
do not add queued/completed replies when a tracked digest can show their feedback.
Failures still send actionable replies; partial successes stay checked off.
Newer cloud task changes take precedence over older mailbox acknowledgements.

Sent digests retain their original rows, date, and section membership. Section
and summary totals count remaining open/waiting rows in those original sections;
waiting/reopened tasks show their current state in place. Fresh digests regroup
tasks normally and omit done/dismissed tasks. Earlier digests remain history.

If a task has no tracked digest, or Telegram rejects an edit because a digest was
deleted or cannot be edited, the bot sends a concise fallback reply. Telegram
cleanup is best effort: commands older than 48 hours cannot generally be deleted,
and failed deletion does not undo the action. Other commands, including `/list`,
`/sync`, and `/retry`, keep their existing replies. Local listener mode is unchanged.

When upgrading, apply D1 migration `0003_digest_interactions.sql` before deploying
the Worker. Previously sent digests cannot be edited because their Telegram IDs
were not saved; use `/list` to obtain a tracked digest. No local database change
or bridge update is required for this feature.

The bridge polls every 30 seconds and synchronizes once per Singapore-time
four-hour slot: **00:00, 04:00, 08:00, 12:00, 16:00, 20:00**. Starting or resuming
it catches up in the current slot without replaying missed slots. Explicit `/sync`
and `/list` bypass this interval. Scheduled sync failure is retried next slot;
send `/sync` for an earlier retry.

Cloud Cron runs every five minutes and sends the daily digest at the first run at
or after **08:00 Asia/Singapore**, without waiting for Windows. It uses the latest
mailbox snapshot and current cloud task states. Sync timestamps and classification
warnings remain visible. Category totals describe the 24-hour window at snapshot
preparation, even if Windows has been off for days.

## Cutover

The current deployment uses **https://noelle-outlook-triage-bot.pages.dev** for
HTTP requests because the original workers.dev address does not resolve on this
network. Pages only forwards requests through a `BOT` service binding to
`outlook-triage-relay`. All secrets, database access, command handling, and Cron
remain in that original Worker; Pages stores no copy of the credentials.

To deploy this alternative ingress, run from `relay/pages/`:

```powershell
npx wrangler pages project create noelle-outlook-triage-bot --production-branch main
npx wrangler pages deploy public --project-name noelle-outlook-triage-bot --branch main --commit-dirty=true
```

Create the project only once. Set the local `OUTLOOK_TRIAGE_RELAY_URL` to the Pages
address, and use that same address for the Telegram webhook. The Worker Cron
continues to work without a publicly resolvable workers.dev route. Pages Functions
share the Workers Free request allowance; no paid subscription is introduced.

Complete the original [relay setup](Cloud-Relay.md) first. Keep Workers on the Free
plan. D1 is the durable command queue and encrypted response outbox; no separate
queue service is needed.

1. Stop local writers and back up SQLite. Preserve its new `bridge_state` and
   `bridge_receipts` tables in subsequent backups. The stable dataset ID binds
   this database to the cloud and prevents task ID collisions with another database.
2. In `relay/`, run `npm ci`, `npm run typecheck`, and `npm test`, then apply:

   ```powershell
   npx wrangler d1 migrations apply outlook-triage-relay --remote
   ```

3. Generate an independent random secret with at least 32 URL-safe characters.
   Store it in your password manager and add it with
   `npx wrangler secret put TELEGRAM_WEBHOOK_SECRET`. Do not reuse existing tokens.
   Optionally store the same `TELEGRAM_WEBHOOK_SECRET` in local `secrets.env` to
   enable unattended webhook setup; otherwise the command prompts for it.
4. Set `CLOUD_BOT_ENABLED` to `"true"` in `relay/wrangler.jsonc`, then deploy with
   `npx wrangler deploy`. The flag selects exactly one digest sender: old relay or
   cloud bot.
5. Add `OUTLOOK_TRIAGE_CLOUD_BOT=true` to local `secrets.env`. Keep the existing
   relay URL, token, and encryption key. Local CLI mutations, Telegram delivery,
   polling, and legacy sync commands now refuse to bypass cloud ownership.
6. From the project root, preview `scripts/register_cloud_bridge.ps1 -ShowPlan`,
   then run it without `-ShowPlan`. It stops/disables this installation's legacy
   Sync, Digest, and Telegram Commands tasks, registers Cloud Bridge at logon, and
   starts it. Stop any separate foreground listeners or sync processes too.
   The initial publication imports existing classifications and task numbers.
   Connectivity and webhook-secret checks run before any schedule changes. The
   background task uses `pythonw.exe`, so no console window needs to stay open.
7. Run `outlook-triage cloud-webhook-enable`. It verifies bridge connectivity,
   prompts for the hidden secret from step 3, installs the cloud command menu,
   and verifies the webhook URL. Pending Telegram updates are preserved.
8. Test `/status`, `/tasks`, `/waiting`, and `/reopen`. Verify that subsequent
   bridge uploads preserve cloud changes. Test `/read` on a non-sensitive message.

### Optional cached body previews

Use `-CachePreviews` with the scheduler, or `bridge-listen --cache-previews`, to
upload bounded original email text for `/show`. Default: off. This additionally
sends original body text to Cloudflare and, on request, Telegram. Attachments are
never read. Disabling the option and publishing removes previews from the current
snapshot; previous Telegram messages and pending frozen responses may retain them.

## Reliability, privacy, and recovery

Task content, subjects, senders, previews, and replies are encrypted at rest using
AES-256-GCM. The Worker holds the decryption key. Task IDs/statuses, command types,
referenced IDs, progress, dataset identity, and timestamps remain operational
metadata in D1. Raw command text is not retained; logs use sanitized categories.

Cloud task statuses override uploaded local statuses after the initial import.
Manage tasks in Telegram. Use one bridge and the same local database; do not run
independently modified copies. Back up both local SQLite and cloud D1.

Queued commands and per-item progress have no 24-hour expiry. Frozen encrypted
responses and digest edits resume after network failure on a subsequent webhook,
bridge, or Cron request. Edits are rendered from current state when delivered;
Telegram rate-limit retry delays are respected. Telegram has no sendMessage
idempotency key: a lost acceptance response can duplicate one chunk. Delivered
responses are deleted after 30 days. Encrypted digest layouts and Telegram IDs are
also retained for 30 days, with cleanup deferred while output is pending. Update
deduplication records remain. Monitor free-tier storage as history grows.
After an interrupted process, the bridge verifies the recorded process is dead
before recovering its lock; it does not displace a live bridge.
Free-tier resource limits can pause service; no paid plan is enabled or assumed.
Permanent Telegram rejection leaves output pending until credentials/chat access
are corrected. A permanently failing first reply can delay later replies.

For acceptance, keep Windows off for 48 hours and exercise cloud commands, queued
mailbox commands, and both morning digests. Reconnect and verify queued changes
and digest checkmarks against Outlook. Automated tests do not replace this test.

For rollback, first let the bridge drain commands and mirror cloud task changes
into SQLite. Stop/disable Cloud Bridge, run `cloud-webhook-disable`, set the local
cloud setting to false, set the Worker flag to false and redeploy. Re-enable the
original Windows schedules and run `relay-publish`. If Windows cannot reconnect,
preserve cloud D1 first: newer edits and pending work are not yet in local SQLite.
