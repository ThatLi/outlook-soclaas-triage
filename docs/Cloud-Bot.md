# Advanced: existing cloud bot and Windows bridge

For a first installation, use [local Setup](User-Guide.md). Cloud resources must be created and maintained separately by an experienced owner; the installer creates none. Setup can connect an existing deployment using its HTTPS URL and independent secrets. It never changes a webhook, publishes a snapshot, or assumes dataset ownership. Original preview caching is off.


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

### Quiet task feedback

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

## Connecting an existing deployment

The owner must already have a Worker, D1 schema through migration 0008, Telegram bot/private chat, independent relay bearer token, 32-byte encryption key, and independent webhook secret. Use the owner's approved HTTPS ingress. No shared public deployment is supplied. This repository's developer deployment configuration is not your deployment configuration; never reuse another person's database ID, address, bot or credentials.

For a new advanced deployment, the [outbound relay guide](Cloud-Relay.md) describes resource creation separately. The current Worker defaults to cloud bot mode and Cron every minute. Configure `CLOUD_BOT_ENABLED=true` for this mode and apply all migrations, then provide the additional `TELEGRAM_WEBHOOK_SECRET`. No automatic provisioning/template is provided by the installer. Match the ingress used for Windows requests and `/telegram/webhook`; an optional Pages service binding must point to the owner's Worker. Never copy personal ingress names from another deployment.

1. Keep local writers/listeners stopped while confirming ownership. The same SQLite database's stable bridge dataset ID must match the deployed snapshot. Preserve both SQLite and cloud D1; do not delete/recreate a database to bypass an ownership conflict. A fresh local dataset cannot safely replace someone else's cloud task state.
2. In Setup choose **Advanced: connect an existing cloud bot**. Enter the owner's HTTPS URL and secrets through hidden fields. Setup verifies the cloud flag and webhook-secret fingerprint using a read-only GET. It cannot verify the remote encryption key, Telegram bot/chat match or dataset by that GET; confirm these with the owner before proceeding.
3. Setup validates your bot and requires an explicit private-chat confirmation. With an existing webhook, it does not call `getUpdates`; enter the existing private chat ID through hidden input. The webhook must already match the configured ingress plus `/telegram/webhook`. If it is not configured yet, exit Setup and have the owner complete step 4. Do not silently replace another receiver.
4. Advanced owner action only: `outlook-triage cloud-webhook-enable` verifies the bridge dataset through existing bridge logic, checks the independent webhook secret, configures Telegram's command menu and confirms its webhook without dropping pending updates. For a genuinely new, owner-approved empty deployment, initialize/publish the correct local dataset with existing `bridge-poll` after explicitly approving real mailbox processing; this can read and classify email. Never use it as a beginner connection test or against an unconfirmed existing dataset.
5. Complete Setup and **First-run Check** before enabling background operation. This uses fictional classification plus confirmed harmless Telegram delivery. The background bridge runs windowlessly, under the interactive Windows user, at logon and immediately after final guided consent. It disables the same installation's opposite-mode tasks, without touching foreign installations. Keep other foreground listeners/writers stopped.
6. Test `/status` and `/tasks`, confirm fresh bridge timestamps, and compare cloud task state with the owner. Use `/list` only after approving real mailbox processing. Mailbox changes wait for the bridge; cloud task management remains available while Windows is off.

Advanced source scheduling uses `scripts/register_cloud_bridge.ps1 -ShowPlan`, then the same script without `-ShowPlan` after First-run Check. Registration alone starts at the next logon; the guided flow explicitly starts the listener. `-CachePreviews` remains an explicit advanced opt-in. Installed users should use the Start shortcuts.

### Optional cached body previews

Use `-CachePreviews` with the scheduler, or `bridge-listen --cache-previews`, to
upload bounded original email text for `/show`. Default: off. This additionally
sends original body text to Cloudflare and, on request, Telegram. Attachments are
never read. Only open/waiting tasks and confirmed unread informational emails
received within the last 72 hours receive previews. Closed tasks, read/expired
informational emails, and inaccessible Outlook items have no preview in the next
publication. Other metadata and summaries remain available.

The cloud also removes closed-task previews after Outlook confirms completion,
and removes expired informational previews through its existing minute schedule,
including while Windows is offline. `/show` refuses ineligible cached previews
immediately. Cleanup preserves the original snapshot timestamps and encryption.
Reopening a task requires a new bridge publication to restore its preview.

Disabling caching and publishing removes all previews from the current snapshot.
Already sent Telegram messages are unchanged. Previously requested `/show` replies,
including pending replies, retain the existing delivery and 30-day post-delivery
cleanup policy; they are separate from the current preview cache.

Successful `/show` previews are temporary chat messages. The next valid command
in the configured private chat, including another `/show`, deletes the previous
preview's parts and its `/show` command. Unsent older preview parts are cancelled.
The daily brief, errors, and the new command's response are preserved. Retries
and Telegram's deletion age limits still apply; previews sent before this update
were not tracked and cannot be removed automatically. This does not erase the
underlying eligible preview cache.

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

For an intentional return to local mode, first let the bridge drain commands and mirror cloud task changes
into SQLite. Stop/disable Cloud Bridge, run `cloud-webhook-disable`, set the local
cloud setting to false, set the Worker flag to false and redeploy. For purely local operation, the owner must also clear all three saved relay connection values after preserving a private backup. Reopen Setup, confirm local mode, complete First-run Check and enable local schedules. If retaining the legacy outbound relay instead, use the advanced scheduler and `relay-publish`. If Windows cannot reconnect,
preserve cloud D1 first: newer edits and pending work are not yet in local SQLite.
## Informational digest support

Deploy the updated relay before upgrading and reconnecting the Windows bridge.
No cloud database migration is required for informational reads; their separate
internal action uses the existing command queue and completion receipts.

The brief includes unread informational emails received within the last 72 hours.
Use `/show #i42` for a cached original-body preview and `/read #i42` (or several
informational references) to queue marking them read. Task IDs and informational
IDs must be sent separately. Informational emails do not support task workflow
commands such as `/done` or `/waiting`.

Entries stay visible while a read is queued. The Windows bridge publishes confirmed
read status before acknowledging completion. Partial successes are retained and
retries are safe. Reading directly in Outlook is reflected after synchronization.
Cloud previews require `--cache-previews`; summary delivery does not.

Informational `/read #i42` follows quiet task feedback: the current digest shows
read progress, then removes confirmed read entries. Covered commands do not send
separate queued/marked replies. The accepted command is deleted only after its
feedback is visible, and retries preserve partial successes. If no editable
current digest covers an informational reference, a concise fallback identifies
the `#i` references. Failures and `/show` previews remain visible. Deletion uses
the existing age limits and retry handling. Old notices sent before this update
were not tracked for cleanup and may need manual deletion.

New editions select the current 72-hour window. An existing edition preserves its
original summaries and membership, while confirmed reads can remove entries from
current editions. Older delivered editions remain frozen.
