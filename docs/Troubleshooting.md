# Troubleshooting

Open **Start → Outlook SoCLaaS Triage → Outlook Triage - Troubleshooting**.
Keep classic Outlook open and your network connected. Allow a few minutes for
network retries. Results stay visible until you press Enter to close the window.
No messages are sent, no Telegram updates are consumed, no email items/bodies are
read, no database is opened/migrated, and no schedules are changed. Outlook itself
may open or ask permission. An optional report is saved only when you choose it.

**PASS** means that check succeeded; **FAIL** needs recovery; **WARN** commonly
means background operation has not been enabled or its state needs attention.
Each failure explains its effect and numbered recovery steps. Resolve failed
connections before enabling background processing.

| Symptom | Effect | Recovery steps | Check the fix |
| --- | --- | --- | --- |
| New Outlook; classic Outlook unavailable | No mailbox connection. | 1. Look for the **File** tab. 2. Open **Outlook (classic)** from Start. 3. If missing, ask IT to install/configure it; then sign in. | Reopen Setup; confirm the profile and mailbox. Troubleshooting's **Classic Outlook/profile** should PASS. |
| Wrong mailbox/profile | The app could read the wrong Inbox. | 1. Open **Stop Background Operation**. 2. Close Outlook; open the intended profile. 3. Reopen Setup, enter a named profile if needed (`-` clears a saved name), and confirm the displayed mailbox. | Run First-run Check before re-enabling; check the displayed profile, then use `/status`. |
| Missing/rejected SoCLaaS key; HTTP 401/403 | Classifications cannot complete. | 1. Open Setup's SoCLaaS step. 2. Obtain your own key through **Get My API Key** at [SoCLaaS](https://soclaas.comp.nus.edu.sg/) on the NUS network. 3. Paste it into hidden input. Without access, ask your organization/service administrator. | **SoCLaaS/model** should PASS; First-run Check should classify the fictional sample. |
| Configured model unavailable or fictional test fails | Listing succeeded but classification is unavailable/incompatible. | 1. Reopen Setup. 2. Choose another available chat model supporting structured JSON; ask the service administrator if unsure. 3. Retry First-run Check. | Expect **First-run Check passed**. A list of model IDs alone does not prove compatibility. |
| Invalid Telegram token; HTTP 401 | No Telegram access. | 1. Open the exact official **@BotFather** via [this link](https://t.me/BotFather). 2. Obtain the token for your intended bot. 3. Paste it into Setup's hidden token field and check the bot username. If a token leaked, replace it through BotFather. | **Telegram** should PASS; request the harmless test and verify its arrival. |
| **No recent private chats found** | No chat can be authorized yet. | 1. Open the exact bot username shown by Setup. 2. Unblock it and send `/start` privately (not in a group). 3. Stop any other receiver for that bot. 4. Choose **Retry after sending /start**, then **Find private chats**. No short timer applies. | Select the intended chat and explicitly choose **Yes, authorize this chat**. |
| Test message not received | Delivery or destination may be wrong. | 1. Check internet connectivity. 2. Open/unblock the bot and send `/start`. 3. Reopen Setup and confirm the intended bot and private chat. 4. Send test again; inspect the private chat, not a group. | Message should say **Outlook Triage Telegram delivery is configured successfully.** Choose Yes in First-run Check only after seeing it. |
| Existing webhook conflict | Local polling cannot share the bot with a webhook. | 1. Identify the deployment that owns the bot. 2. Keep its webhook unchanged if it is still in use; create a dedicated bot for local installation. 3. If you own it and intend to switch, follow [Advanced Cloud recovery](Cloud-Bot.md) deliberately, retaining pending updates. | **Webhook** should PASS. Setup/doctor never delete webhooks or discard updates. |
| Network/proxy restrictions; unreachable service, HTTP 429 or 5xx | Checks/classification/delivery may fail temporarily. | 1. Check your approved connection/VPN. 2. Retry later for rate limits or outages. 3. Ask IT about access to SoCLaaS, `api.telegram.org`, and your cloud endpoint if used. Use only your organization's approved proxy setup. Never share credentials in a screenshot or support ticket. | Reopen Troubleshooting; the service check should PASS. Then rerun First-run Check if necessary. |
| Background task not running; bot does not reply | No automatic processing or command response. | 1. Stay signed in and awake; open classic Outlook and close blocking dialogs. 2. Open Troubleshooting. 3. Run First-run Check and choose **Enable background operation**. 4. In Task Scheduler check this installation's **Actions**, **Last Run Result**, **Last Run Time**, and **Next Run Time**. Ask IT if task registration is denied. | **Background tasks** should PASS. Send `/status`; then check the last sync after a timed run. Registration alone does not prove execution. |
| Stale digest/no eligible messages | Brief may be old or correctly empty. | 1. Check `/status` and digest's last successful sync time. 2. When approved to process email, send `/list` (it sends eligible email text to SoCLaaS). 3. Confirm messages are in this profile's Inbox, unread or flagged, within the initial seven-day scan or subsequent sync overlap, and not excluded by filters. 4. If status reports failed classifications, resolve service errors and send `/retry`. Informational entries must be unread and within 72 hours; done/dismissed tasks do not appear in fresh briefs. | Compare the new timestamp and counts with Outlook. An empty digest can be valid. Do not delete the database to force a rescan. |
| Outlook access denied by policy | Outlook automation cannot operate. | 1. Keep security protections enabled. 2. Record the safe **Classic Outlook/profile** failure from Troubleshooting. 3. Ask your organization's administrator to review the Outlook Programmatic Access policy and approve an allowed integration. | Retry Troubleshooting after administrator action. Do not run everything as administrator or bypass policy. |
| Setup says settings cannot be read | Invalid advanced configuration can prevent loading. | 1. Use **Settings Folder** to locate the private settings file. 2. Ask a trusted administrator to correct invalid numbers, timezone or permissions; retain a private backup. 3. Do not paste the file into public support. | Reopen Setup and Troubleshooting; **Configuration** should PASS. |
| Existing cloud cannot connect/ownership mismatch | Bridge and cloud commands may fail; another dataset must not be overwritten. | 1. Keep current settings and database. 2. Ask the deployment owner to confirm independent credentials, bot, private chat, HTTPS URL and dataset. 3. Follow [Advanced Cloud](Cloud-Bot.md); never create a fresh database as a workaround. | Cloud/Webhook checks should PASS, then owner verifies bridge/dataset behavior without replacing cloud task state. |

## Save a safe support report

1. Open **Outlook Triage - Troubleshooting** and read the results.
2. At **1 Save this report locally; 2 Finish [2]**, enter **1**.
3. The report contains application version, UTC creation time, fixed check names,
   PASS/FAIL/WARN and safe HTTP status codes. It contains no credentials, raw
   errors, email content/addresses, Telegram chat IDs, profile names or personal
   paths. It is not a copy of application logs or settings. The exact report
   contents are described before saving.
4. The window shows **Saved:** and the full file path. Open **Outlook Triage -
   Reports and Logs**, then `reports`, and find `diagnostic-...json`. Alternatively
   paste the displayed folder path into File Explorer. The default is
   `%LOCALAPPDATA%\OutlookTriage\reports`.
5. Open the JSON file in Notepad and review it before sharing. There is no
   established project support contact; use only a maintainer channel you have
   independently verified. The app never uploads the report automatically.

Technical detail is deliberately bounded to safe check names and HTTP codes.
Ordinary logs are under `%LOCALAPPDATA%\OutlookTriage\logs`; they may contain
operational metadata. Do not share ordinary logs, the database, credentials file
or digest reports as substitutes for the sanitized diagnostic report.

The folder access check reads permission metadata without creating a test file;
actual ability to write can still be affected by policy or disk space. A chat
lookup cannot prove delivery. A model listing cannot prove structured generation
or useful classification. A cloud GET cannot prove dataset ownership or validate
the remote encryption key. Task registration does not prove a future scheduled
run. First-run Check and the daily-use checks cover those limitations where safe.

## Advanced exit codes

| Command | Code | Meaning |
| --- | --- | --- |
| `setup`, `verify` | 0 | Completed the connection/test flow. The user can still choose to keep background operation off. |
| `setup`, `verify` | 1 | Incomplete or failed required checks. |
| `setup`, `verify` | 130 | Exit, Back out of the flow, Ctrl+C or unavailable input. Saved completed settings remain. |
| `doctor` | 0 | All checks PASS. |
| `doctor` | 1 | At least one required check FAILs. |
| `doctor` | 2 | Only warnings, usually background registration or task state. |
| `doctor` | 130 | Interrupted before completion. |
| Other commands | 0 / 1 / 2 / 130 | Success / operational failure / failed classifications or argument syntax error / interruption. See [CLI reference](Advanced-CLI.md). |

`doctor` is noninteractive by default and does not save files. `doctor --pause`
offers the optional report and waits before closing, as the shortcut does.
