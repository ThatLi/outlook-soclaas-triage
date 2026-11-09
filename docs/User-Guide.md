# Install and use Outlook–SoCLaaS Triage

This app reads eligible messages from your classic Outlook Inbox, asks SoCLaaS
to identify actions and deadlines, and prepares a brief for your private Telegram
chat. You use your own bot. Local operation is recommended for the first release.

## Before you install

You need:

1. A Windows 10 or 11 **x64** computer with a supported, signed-in **classic Outlook** installation and a configured Inbox. Keep Windows and Office supported by your organization. The installer has not been validated on ARM or 32-bit Windows.
2. Permission from your organization to send the data described below externally.
3. A SoCLaaS API key. This private credential gives the app access to models. [SoCLaaS](https://soclaas.comp.nus.edu.sg/) serves the NUS School of Computing community. Choose **Get My API Key** on that site from the NUS network. If you lack access, ask your organization or service administrator before continuing. This app provides no public signup or substitute service.
4. A Telegram account and permission to create your own bot. Setup explains how.
5. An internet connection that can reach SoCLaaS and Telegram. Corporate networks may require an approved VPN or proxy; ask IT rather than changing security protections.

Classic Outlook has a **File** tab. New Outlook cannot provide the connection this
app uses. Search Start for **Outlook (classic)**. If unavailable, ask IT to install
and configure it. Open it and confirm the intended Inbox before Setup.

## Understand the data transfers

Once you explicitly enable processing, the app sends sender information, subject,
received date and cleaned, bounded email text to SoCLaaS. It skips attachments;
cleaning does not guarantee removal of every sensitive detail. SQLite stores
message metadata, classifications and task history locally. Reports and delivery
queues can contain summaries and personal information.

Telegram receives task descriptions, summaries, senders, deadlines and the commands
you send. `/show` additionally sends a bounded original email body preview. Normal
sync does not mark mail read. Your authorized `/read`, `/done` and `/dismiss`
commands **do mark the original emails read**. The app never sends, moves, flags
or deletes Outlook messages. Do not use commands on sensitive messages until
your organization approves these transfers.

Setup checks connections and profile metadata, without classifying mailbox
messages. Original email preview caching for advanced cloud mode is **off**.
Cloud operation adds transfers to a deployment you control; see the separate
[Advanced Cloud guide](Cloud-Bot.md). Setup never creates cloud resources.

## Download and install

1. On the [project's GitHub Releases page](https://github.com/ThatLi/outlook-soclaas-triage/releases), open version **1.0.0** and expand **Assets**.
2. Download **Outlook-SoCLaaS-Triage-1.0.0-windows-x64-setup.exe**. Do not download **Source code (zip)** or **Source code (tar.gz)**. You do not need Python.
3. Open the downloaded installer. Read any Windows security notice. This build is unsigned unless the publisher explicitly says otherwise. If Windows or your organization blocks it, ask IT to review the download; do not disable protections or bypass your organization's policy.
4. Choose your account's installation folder (the default is suitable), choose **Next**, then **Install** and **Finish**. Administrator access should not be needed. Keep **Open Outlook Triage Setup** selected, or open **Outlook Triage - Setup** from Start afterwards.
5. If installation reports that background tasks cannot stop, open **Outlook Triage - Troubleshooting**, resolve task access with IT, and retry. Existing settings and data are retained.

The installer creates a Start Menu folder called **Outlook SoCLaaS Triage**.
Spaces and non-English characters are supported in the installation path.

## Complete Setup

The numbered text window accepts choices by number followed by Enter. Press Enter
for a displayed default. Type **Back** to revisit an earlier step or **Exit** to
stop. A secret field hides typing, including Back/Exit. Saved valid values can be
kept with Enter; secrets are never displayed. There is no time limit for the
Telegram steps. Results stay visible until **Press Enter to close this window**.
Do not close the window until you have read them.

If background tasks already exist, Setup asks permission to stop this
installation's tasks first. Choose **Stop this installation's background tasks**
to review settings, or **Exit without changes**. You will enable processing again
only after First-run Check.

1. **Windows and your mailbox.** Open classic Outlook first. Leave the profile name blank to use the current/default profile. If you deliberately use a named profile, enter that name. Setup shows the actual connected profile and Inbox mailbox, without opening email items. Confirm **Yes, use this mailbox** only if both are right. Otherwise choose **No, go back**, close Outlook, reopen the intended profile and retry. Enter `-` in the profile field to clear a saved name.
2. **Connect SoCLaaS.** Paste your key into hidden input. Setup checks access and retrieves the models available to that key. Choose a chat model supporting structured JSON by number. Ask your service administrator which to use if unsure; embedding/audio models are unsuitable. A saved available model can be kept without re-entering it. Model availability alone does not establish compatibility; First-run Check tests fictional structured classification.
3. **Choose where the application runs.** Choose **Local (recommended)**. Your computer must be available for the bot to respond. **Advanced: connect an existing cloud bot** is for an owner who already has a deployment and its independent secrets. Existing cloud ownership cannot be switched to local automatically; follow [Advanced Cloud recovery](Cloud-Bot.md) first.
4. **Connect your own Telegram bot.** Open the official [@BotFather](https://t.me/BotFather), check the exact username and verified account, send `/newbot`, and follow its name/username instructions. Copy its token into Setup's hidden field. Setup displays the bot's username; confirm it is yours. Use a dedicated bot. An existing webhook stops local setup with recovery instructions; Setup will never remove it or discard pending updates. In Telegram, open your new bot and send `/start` in a private chat. Return to Setup and choose **Find private chats**. Select your chat by number and explicitly choose **Yes, authorize this chat**. A chat is never authorized merely because it arrived first. If no chat appears, follow the displayed instructions and retry after sending `/start` again. You can send the harmless test; it says **Outlook Triage Telegram delivery is configured successfully.** No email is included.
5. **Final connection checks.** Expect PASS for required checks and **Setup connections complete. No mailbox processing was started.** If a check fails, read its numbered recovery steps, choose Retry where offered, or reopen Setup later. Completed settings remain saved. Background processing stays off. Next open **Outlook Triage - First-run Check** from Start.

## Separate safe First-run Check

1. Open **Outlook Triage - First-run Check**. It repeats connection checks, without reading messages.
2. Read the explanation, then choose **Run fictional test**. Only fictional installation text is sent to your selected model. The model must return a valid classification. No classification or task is saved from the sample.
3. Check your private Telegram chat for the harmless test message quoted above. Choose **Yes, I received it** only if you received it in the intended chat. Otherwise choose **No**, open Troubleshooting and recover before continuing.
4. Expect **First-run Check passed: connections, fictional classification and confirmed Telegram delivery.** Read the separate background consent carefully. No real email has been processed by the test.
5. Choose **Keep background operation off** if you are not ready. Reopen First-run Check later to enable it. Choose **Enable background operation** only when you approve the real email transfers. Scheduled work that was missed earlier today may start immediately. The listener starts now; cloud mode's bridge may start processing now.

Changing a connection setting requires First-run Check again. A fictional test
verifies output format and delivery, not the quality of classifications on your
mailbox. Review real results before relying on them for deadlines.

## Confirm background operation and begin daily use

1. After enabling it, send `/status` to your bot. Expect a status reply with sync and task counts. Before the first successful sync it may say the app has never synchronized; this is expected. `/status` itself does not read email.
2. When ready to send eligible real email to SoCLaaS, send `/list`. This synchronizes your Inbox and sends a current brief. The initial scan looks back seven days and includes unread or flagged eligible messages; it does not mean only one test email. Check the timestamps and results against Outlook. Do not issue `/read`, `/done` or `/dismiss` as a test unless you intend to change read state.
3. Open **Outlook Triage - Troubleshooting** and review **Background tasks**. A PASS confirms registration, enabled tasks and program paths, not that a future run has succeeded. To check actual scheduled execution, open Windows **Task Scheduler**, select **Task Scheduler Library**, find tasks beginning **Outlook SoCLaaS Triage** with this installation's identifier, and check **Last Run Result**, **Last Run Time**, and **Next Run Time**. `0x0` means success; a continuously running listener shows Running. If multiple installations exist, the **Actions** tab identifies the installation folder.
4. After the next timed sync, send `/status` again and check the last successful synchronization time. If no response or stale results, follow [Troubleshooting](Troubleshooting.md).

| Telegram command | What happens in recommended local mode |
| --- | --- |
| `/help` or `/start` | Shows commands. |
| `/status` | Shows saved sync and task status; no mailbox processing. |
| `/tasks` | Lists saved tasks. |
| `/list` | Synchronizes and sends a fresh digest; sends eligible email text to SoCLaaS. |
| `/sync` | Synchronizes without a digest. |
| `/retry` | Retries pending/failed classifications. |
| `/waiting #42`, `/reopen #42` | Changes saved task state only. |
| `/read #42` | Marks the task's original email read. |
| `/done #42`, `/dismiss #42` | Marks the original read and completes/dismisses its task. |
| `/show #42` | Sends a bounded original email preview to Telegram. |

Only the private chat you confirmed can issue commands. Task numbers appear in
your digest. Informational entries use references such as `#i42`; `/show #i42`
and `/read #i42` work too. See [Advanced CLI](Advanced-CLI.md) for full semantics.

## Times and computer availability

Local synchronization runs at **03:50, 07:50, 11:50, 15:50, 19:50 and 23:50**;
the Telegram digest runs at **08:00**. These are **Windows local clock times**.
Report dates and deadlines use **Asia/Singapore** by default, independently of the
Windows clock setting. Advanced users can change the report timezone. The cloud
bridge and cloud daily digest use Singapore time as documented separately.

Tasks run with ordinary user privileges in your signed-in session. Locking the
screen keeps the session signed in, so tasks can run if Outlook is responsive;
blocking prompts may prevent access. Sleep/off/sign-out prevents local operation.
Tasks do not wake the computer. Missed timed tasks start when the computer and
user session become available; missed intervals are not replayed individually.
The listener starts when enabled and at sign-in. Commands Telegram still retains
can execute after the listener resumes; avoid queuing commands you no longer want.
No background program can access Outlook while you are signed out. Cloud cannot
read new mailbox data until its Windows bridge returns.

## Reopen, stop, find files, uninstall

- Reopen **Outlook Triage - Setup** to review connections; it preserves unrelated settings. There is no automatic updater.
- Open **Outlook Triage - Troubleshooting** to run read-only checks and optionally save a sanitized report. The report is never uploaded.
- Open **Outlook Triage - Stop Background Operation** to remove only this installation's tasks and stop their running instances. Close any separate foreground developer listener too. Re-enable through First-run Check. Stopping Windows tasks does not stop an advanced cloud deployment's own delivery.
- Open **Outlook Triage - Settings Folder** to find private settings and filter rules. They normally live at `%APPDATA%\OutlookTriage`.
- Open **Outlook Triage - Reports and Logs** to find the `reports`, `logs`, and `state` folders and SQLite database. They normally live at `%LOCALAPPDATA%\OutlookTriage`. You can also press Windows+R, paste either folder path and press Enter. Do not share the credentials file, database, ordinary reports or logs publicly. Advanced path overrides can relocate these files; Setup shows the effective locations.
- To uninstall, open Windows **Settings → Apps → Installed apps** (Windows 10: **Apps & features**), find **Outlook SoCLaaS Triage**, and choose **Uninstall**. Its owned tasks and installed shortcuts are removed. Settings, credentials, SQLite data, reports and logs remain. Reinstall retains them and leaves background operation off until you enable it again. Keep the same install path; moving to another path does not give the new installation ownership of old tasks.

There is no established project support contact. Ask your organization about
access/policy issues. For a software issue, collect the sanitized diagnostic
report following [Troubleshooting](Troubleshooting.md), review it, and share it
only with a maintainer through a channel you have independently verified.
