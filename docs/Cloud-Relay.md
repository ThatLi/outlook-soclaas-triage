# Cloudflare Telegram Relay

This optional phase-one relay sends the newest digest prepared on Windows at 08:00 Asia/Singapore even if Windows is then off. It does not access Outlook, classify messages, receive Telegram commands, or make cloud state authoritative.

The local application formats the digest and future cached command views, encrypts the complete payload with AES-256-GCM, and uploads only ciphertext plus routing timestamps. Cloudflare decrypts the frozen bundle only when sending it to Telegram. D1 retains the newest bundle indefinitely, removes superseded unreferenced bundles after seven days, and removes completed delivery records after 30 days.

## 1. Create independent secrets

Run the following twice in PowerShell. Use one result as the relay bearer token and the other as the encryption key:

```powershell
$bytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
```

Do not reuse the Telegram bot token as either relay secret. Store the values in a password manager. The encryption value must decode to exactly 32 bytes.

## 2. Create the free Worker and D1 database

Install Node.js, then run from the repository:

```powershell
Set-Location .\relay
npm ci
npx wrangler login
npx wrangler d1 create outlook-triage-relay
```

Copy the returned database ID into `relay\wrangler.jsonc`, replacing `REPLACE_WITH_D1_DATABASE_ID`. Apply the schema:

```powershell
npx wrangler d1 migrations apply outlook-triage-relay --remote
```

Add the four Worker secrets. Wrangler prompts for each value without writing it to the repository:

```powershell
npx wrangler secret put RELAY_TOKEN
npx wrangler secret put RELAY_ENCRYPTION_KEY
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
```

Use the same private Telegram bot token and chat ID as the local application. The Worker only calls `sendMessage`; it never calls `setWebhook` or consumes updates.

Deploy and record the resulting `https://...workers.dev` address:

```powershell
npm run typecheck
npm test
npx wrangler deploy
```

Cloudflare Cron invokes the Worker every 30 minutes. Before 08:00 Singapore time it exits without creating a delivery. At or after 08:00 it freezes that day's newest bundle and sends at most 20 messages per invocation until complete.

## 3. Configure Windows

Add the following to `%APPDATA%\OutlookTriage\secrets.env`:

```dotenv
OUTLOOK_TRIAGE_RELAY_URL=https://YOUR-WORKER.workers.dev
OUTLOOK_TRIAGE_RELAY_TOKEN=YOUR-INDEPENDENT-BEARER-TOKEN
OUTLOOK_TRIAGE_RELAY_ENCRYPTION_KEY=YOUR-BASE64URL-32-BYTE-KEY
OUTLOOK_TRIAGE_RELAY_TIMEOUT_SECONDS=20
```

All three credential settings are required together. Restart any foreground listener after changing the file.

Prepare and upload the first snapshot:

```powershell
outlook-triage relay-publish
outlook-triage relay-status
```

Afterward, each completed `sync` and `retry-failed` prepares a newer bundle automatically. A network or Worker failure prints `relay_pending=1` but does not undo the completed Outlook synchronization. The pending bundle survives restarts.

## 4. Acceptance test

1. Run `outlook-triage sync` and confirm it prints `relay_pending=0`.
2. Run `outlook-triage relay-status` and confirm the local bundle is `confirmed` and matches `latest_bundle` in cloud status.
3. Do not post the status output publicly; timestamps and identifiers are operational data.
4. Turn Windows off before 08:00 Singapore time.
5. Confirm Telegram receives one relay provenance message followed by the digest.
6. Leave Windows off through another 08:00 to confirm the newest frozen digest is deliberately resent with its original timestamps.
7. Turn Windows on, start `telegram-listen`, and confirm `/status` still works without a webhook conflict.

## Rotation and recovery

- **Bearer-token rotation:** run `npx wrangler secret put RELAY_TOKEN`, update the local value, then run `relay-publish`.
- **Encryption-key rotation:** update the Worker secret and local value together. Existing bundles become unreadable, so immediately publish a new bundle. A delivery frozen before rotation cannot resume; inspect D1 and remove that pending delivery before retrying.
- **Telegram-token rotation:** update both the local secrets file and `TELEGRAM_BOT_TOKEN` Worker secret.
- **Permanent Telegram rejection:** correct the bot/chat secrets. The next 30-minute Cron attempt resumes from the recorded chunk.
- **Lost local outbox:** `relay-publish` recreates it from the current SQLite database.
- **Lost D1 database:** recreate it, apply migrations, redeploy, and run `relay-publish`.

Telegram has no idempotency key for `sendMessage`. If Telegram accepts a chunk but the network response is lost, one chunk can appear twice on retry. Confirmed earlier chunks are not deliberately resent.

## Privacy boundaries

D1 stores encrypted payloads, but the Worker holds the decryption key and necessarily decrypts content immediately before sending it to Telegram. Cloudflare metadata includes bundle IDs, intended dates, and sync/generation timestamps. Digest content includes task descriptions, sender information, deadlines, and synchronization health. Confirm that both Cloudflare storage and Telegram delivery are permitted by your organization.
