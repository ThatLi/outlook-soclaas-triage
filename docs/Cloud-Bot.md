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

The bridge polls every five seconds and synchronizes once per Singapore-time
four-hour slot: **00:00, 04:00, 08:00, 12:00, 16:00, 20:00**. Starting or resuming
it catches up in the current slot without replaying missed slots. Explicit `/sync`
and `/list` bypass this interval. Scheduled sync failure is retried next slot;
send `/sync` for an earlier retry.

Cloud Cron runs every minute and sends the daily digest at the first run at
or after **08:00 Asia/Singapore**, without waiting for Windows. It uses the latest
mailbox snapshot and current cloud task states. Sync timestamps and classification
warnings remain visible. Category totals describe the 24-hour window at snapshot
preparation, even if Windows has been off for days.

Each scheduled digest and `/list` digest is a rich message with collapsed priority
sections. Tap a heading to expand its tasks directly in Telegram, without a bot
request or message reload. Headings show counts; empty priorities show zero-count
lines. Task IDs, deadlines, senders, and status annotations remain available.
The date, open-task total, sync timestamp, warnings, and command feedback stay
visible. Recent activity and enabled News are separate collapsed sections.
Large digests arrive immediately as numbered continuation messages, with no
navigation buttons. Each part stays below 30,000 UTF-8 bytes and 400 blocks;
long tasks have labeled fragments retaining their IDs. Keep using typed `/waiting`, `/reopen`, `/done`,
and `/dismiss` commands: all digests from the current Singapore date update
automatically. Telegram may reset expanded sections after an update. `/done` and `/dismiss` update only
after each successful Outlook acknowledgement. Earlier editions keep their last
saved content and remain readable. Previously sent legacy messages retain their
Overview/Outlook/News buttons. `/list` synchronization refreshes its existing
digest. Its pending and completion notices are temporary: both are deleted after
synchronization succeeds and the digest is current. Failed runs keep their
actionable replies. The interactive digest and the user's `/list` command remain.
Failed edits retry on later bot, bridge or scheduled runs. Telegram rate limits
can delay updates. Deleted or inaccessible digest messages are retired without
blocking other deliveries.
Unchanged parts do not trigger a Telegram edit. Successful parts are checkpointed
individually; failed continuations resume without resending completed parts.
Surplus continuations are removed only after replacement content succeeds.

### Quiet task feedback (PR #13)

`/done`, `/dismiss`, `/waiting`, `/reopen`, and `/read` show feedback in today's
interactive digests. Accepted task commands are deleted only after feedback is
delivered. Invalid commands remain visible. Successful task commands do not send
separate queued/completed replies when an editable digest covers the tasks.
`/list` bot acknowledgements are also cleaned up after a successful refresh;
`/sync` and `/retry` keep their existing replies. Failures remain actionable.

| Task state | Appearance |
| --- | --- |
| Open | `☐ #42 Submit report` |
| Done requested | `☐ #42 · ⏳ Done pending Submit report` |
| Done confirmed | `☑ #42 Submit report` |
| Dismissed confirmed | `☒ #42 Submit report` |
| Waiting | `☐ #42 · 💤 Waiting Submit report` |
| Read confirmed | `☐ #42 · Email read Submit report` |

Rows stay in their original sections, including completed and dismissed rows;
totals show remaining open/waiting tasks. A fresh digest regroups tasks and omits
closed tasks. Snapshot refreshes update the original rows' descriptions and
metadata. New tasks appear in the next fresh digest. Visible command feedback
shows progress even when the affected section is collapsed. Command cleanup waits
until all affected digest parts are current. Partial successes keep their checkmarks,
and newer waiting/reopen changes take precedence over older Outlook confirmations.

If no current editable digest contains a task, the bot sends a concise fallback
reply. Earlier editions, including the old multi-message digests, remain history.
Command deletion is best effort: commands older than 48 hours and permanently
rejected deletions are skipped without undoing the action. Transient failures and
Telegram retry delays preserve pending feedback and cleanup for retry.

For the interactive-digest upgrade, apply migration `0003_interactive_digests.sql`
with `npx wrangler d1 migrations apply outlook-triage-relay --remote` before
deploying the Worker. Run `outlook-triage cloud-webhook-enable` with the updated
Python installation to preserve message updates and enable `callback_query`
updates without discarding pending updates. Existing queued chunk deliveries
retain their progress. The old sample navigation-trial buttons are retired.
The migration is additive; retain the new tables when rolling back the Worker.
For the faster-update upgrade, also apply `0004_digest_payload_hash.sql`, deploy
the updated Worker, and restart Cloud Bridge to load the five-second interval.
For restored quiet task feedback, apply `0005_quiet_task_feedback.sql` before
deploying. The original `0003_digest_interactions.sql` migration from PR #13 is
retained for installations that already applied it. Existing interactive messages
acquire their retained layout on first refresh; rows removed before this upgrade
cannot have their original membership reconstructed. No bridge change is needed.

For expandable rich digests, apply `0008_rich_digests.sql` before deploying the
updated Worker. Existing records default to the legacy format; new editions use
rich sections. Keep the additive schema when rolling back. No new dependency,
setting, or Python bridge change is required. Before rollout, inspect a sample on
your daily Telegram clients and confirm expanding sections produces no callback
requests. Automated tests cannot establish client rendering or expansion behavior.
For temporary `/list` acknowledgement cleanup, apply `0006_list_reply_cleanup.sql`
before deployment. Only newly sent tracked notices can be deleted automatically;
previously untracked notice IDs cannot be recovered. Telegram's deletion age
limit and best-effort cleanup rules also apply to these notices.

## Daily news trial

The trial fills a collapsed **News** section in the 08:00 Asia/Singapore daily brief
(the News tab on previously sent legacy editions).
It selects up to five stories posted within the previous 24 hours, preserving
Hacker News ranking among the first ten candidates. This is a technology-focused
trial, not a general or Singapore news service. Headlines link to HN discussions;
they are community submissions, not fact-checked summaries. No article bodies,
mailbox content, or credentials are sent to the news source.

The first digest created on each Singapore date fetches the selection; `/list`
and that day's scheduled brief reuse it. Thus `/list` before 08:00 can set the
morning selection. Saved news is encrypted with the existing relay key and kept
with its digest for the existing 30-day retention period. Older editions retain
their selection when navigated or when task feedback changes.

This approach uses the [official HN JSON API](https://github.com/HackerNews/API),
the existing Cron, D1 storage, Telegram delivery retries, and private-chat access
checks. It needs no new package, API key, Windows uptime, or cloud SoCLaaS access.
There are at most eleven news requests per date, with four-second request
deadlines and 64 KiB response limits. This keeps the trial small within
[Workers Free limits](https://developers.cloudflare.com/workers/platform/limits/);
production CPU usage still needs to be measured. A Windows-based news collector
would miss editions while Windows is off; web search or AI summaries would add
credentials, cost, and additional failure modes.

Enable after reviewing this branch:

```powershell
cd relay
npm run typecheck
npm test
npx wrangler d1 migrations apply outlook-triage-relay --remote
npx wrangler deploy
```

Apply `0007_news_trial.sql` before deployment. `NEWS_TRIAL_ENABLED` is `"true"`
in this branch's `relay/wrangler.jsonc`; set it to `"false"` and redeploy to stop
collecting news for new editions. Previously saved editions remain accessible.
Keep the additive column when rolling back the Worker.

Source outages never block Outlook delivery. A partial response keeps eligible
stories and shows a warning; a total failure saves an unavailable notice for that
date. News fetches retry on the next date, while Telegram delivery continues using
the existing retry path. Empty selections explicitly say no eligible stories were
found. The trial does not add cross-day duplicate suppression or multiple sources.

Review improvements applied after the first implementation: bounded streamed
responses, escaped and decoded headlines, fixed-host discussion links, per-item
failure isolation, and bounded output. Automated coverage includes daily reuse,
date rollover, frozen history, operation without a bridge snapshot, source failure,
source rank, invalid/deleted/stale entries, and hostile or oversized input.

Before judging the trial successful, observe at least two morning deliveries with
Windows off, expand the task and News sections and use `/list`, and check Worker CPU and
errors. Evaluate relevance, empty editions, and source reliability before adding
other publishers or AI summaries. Automated tests do not establish production
free-tier CPU headroom or live Telegram delivery.

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
responses resume after network failure on a subsequent webhook, bridge, or Cron
request. Telegram has no sendMessage idempotency key: a lost acceptance response
can duplicate one chunk. Delivered responses are deleted after 30 days, while
update deduplication records remain. Monitor free-tier storage as history grows.
After an interrupted process, the bridge verifies the recorded process is dead
before recovering its lock; it does not displace a live bridge.
Encrypted digest layouts, message IDs and delivered feedback are retained for
30 days. Cleanup waits until pending deliveries and feedback have drained;
command deduplication records remain. Free-tier resource limits can pause service;
no paid plan is enabled or assumed.
Permanent Telegram rejection leaves output pending until credentials/chat access
are corrected. A permanently failing first reply can delay later replies.

For acceptance, keep Windows off for 48 hours and exercise cloud commands, queued
mailbox commands, and both morning digests. Reconnect and verify queued changes
and completion replies against Outlook. Automated tests do not replace this test.

For rollback, first let the bridge drain commands and mirror cloud task changes
into SQLite. Stop/disable Cloud Bridge, run `cloud-webhook-disable`, set the local
cloud setting to false, set the Worker flag to false and redeploy. Re-enable the
original Windows schedules and run `relay-publish`. If Windows cannot reconnect,
preserve cloud D1 first: newer edits and pending work are not yet in local SQLite.
