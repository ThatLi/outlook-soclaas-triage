# Build and validate the first Windows release

Version: **1.0.0**. Local operation is recommended. An installer build and
automated tests alone do not establish public-release readiness.

## Repeatable build

Use Windows x64, Python **3.14.2** x64, PowerShell **5.1 or 7**, and Inno Setup **6.7.3**.
The end user needs none of these tools. Install the compiler from its official
publisher or your approved package manager; do not disable Windows protections.

From the repository root in Windows PowerShell 5.1 or PowerShell 7:

```powershell
./scripts/build_release.ps1 -Python 'C:\Path To Python\python.exe' -ISCC 'C:\Path To Inno Setup\ISCC.exe'
```

The script builds an isolated environment under `build/release-venv`, installs
the pinned runtime/build dependencies in `packaging/requirements-release.txt`,
checks dependencies and matching package versions, bundles with PyInstaller,
renders an explicit list of guides to offline HTML, smoke-tests the bundle,
and compiles the per-user Inno installer. `-UseExistingEnvironment` is for local
packaging diagnosis only; release publishers should use the isolated default.
Build dependencies are pinned; artifacts are not claimed to be byte-identical
because executable metadata and compiler timestamps can vary.

Outputs:

- `dist/Outlook-SoCLaaS-Triage-1.0.0-windows-x64-setup.exe`
- `dist/Outlook-SoCLaaS-Triage-1.0.0-windows-x64-setup.exe.sha256`

The workflow **Windows release installer** runs tests and this build on a Windows
runner. Run it manually or push a tag matching the package version (`v1.0.0`).
Download its installer artifact. The workflow does not publish to GitHub Releases.
After manual acceptance, create a GitHub Release and attach the installer and
checksum. Checksum verifies file integrity, not publisher identity.

No signing credentials are configured. Treat outputs as **unsigned**. Do not
claim signing or instruct users to bypass SmartScreen/antivirus. If organizational
policy requires signing, approved code signing is a release blocker there.

## Packaging and safety checks

```powershell
./.venv/Scripts/python.exe -m pytest --basetemp=./outputs/release-tests -p no:cacheprovider
./.venv/Scripts/python.exe -m pip check
./.venv/Scripts/python.exe scripts/verify_bundle.py dist/OutlookTriage
```

The installer copies only the PyInstaller output and fixed rendered guide files;
it never copies the checkout, `config`, local cloud settings, runtime folders,
credentials or databases. Bundle verification rejects private runtime/config
file types and runs help and scheduler-plan commands with isolated user paths.
Inspect the artifact contents before publishing. Never use a live data directory
as the build directory. New build tooling should be tested before updating pins.

Task paths are direct program paths; arguments contain commands/options only,
never credentials. Installed tasks use a windowless bundled executable;
development tasks use `.venv\Scripts\pythonw.exe -m outlook_triage`.
Task names include a hash of the installation directory. Ownership requires
matching executable and working directory before replacement/removal. The app
never registers as a service. Uninstall does not remove user data. Reinstall
stops owned tasks before replacing binaries and leaves re-enabling to the user.
Custom task prefixes used by advanced callers must be removed with that same
prefix before uninstall; the installer removes only its default owned names.

## Required manual acceptance before publishing

Use a clean Windows account or VM **without developer Python**, actual classic
Outlook, your own test SoCLaaS key and dedicated Telegram bot. Avoid production
mail. Follow [User Guide](User-Guide.md) verbatim and record Windows/Office versions.

1. Install to a path containing spaces and non-ASCII characters; no administrator prompt. Confirm all Start shortcuts and offline HTML links work.
2. Launch Setup from its shortcut. Try invalid menu input, Exit/Ctrl+C, rerun, invalid keys/tokens, unavailable networks, multiple chat choices, wrong/default/named Outlook profile, and a deliberately occupied webhook. Confirm secrets stay hidden and no update is acknowledged/discarded during discovery. Explicitly deny chat confirmation and verify no chat is authorized.
3. Confirm Setup and Troubleshooting windows stay visible after success/failure/cancellation. Verify no mailbox body reads or task-state changes during Setup. Check Outlook policy-denied recovery on an appropriately restricted test account.
4. Run First-run Check. Deny test receipt once; background must remain off. Repeat and confirm receipt. Only fictional text should be classified before consent. Confirm preview caching stays off.
5. Enable background processing. Confirm no persistent console. Test `/status`, then `/list` after approving real test-message processing. Check scheduled sync/digest and Task Scheduler results at documented Windows-local times. Test a locked session, sleep/resume and sign-out/sign-in. Confirm no waking/service execution while signed out.
6. Run doctor with before/after snapshots of user files, SQLite, Telegram updates and schedules. Only its explicitly requested sanitized report may be created. Review report redaction and offline guide recovery steps.
7. Reinstall with existing settings, unrelated configuration and SQLite history. Confirm data unchanged by the installer and tasks stopped. Re-enable, uninstall, confirm owned tasks/shortcuts removed and other installations' tasks and user files retained. Simulate registration/uninstall failures and check understandable errors.
8. If offering advanced cloud support, verify a matching existing dataset/bot/chat, webhook and secrets with its owner. Do not publish/claim acceptance for cloud ownership transfer based on the read-only GET alone. Creating cloud resources is outside beginner setup.

Keep a record of automated test/build results and precisely which manual checks
were performed. Mark unperformed clean-account, Outlook, Telegram, timed-run and
policy tests as pending; do not infer them from mock coverage.

## Verification recorded on 9 November 2026

- 197 Python tests passed on Windows 11 x64 with Python 3.14.2, including existing application behavior and focused release tests. Dependency consistency checks passed. All PowerShell scripts parsed under Windows PowerShell 5.1. Relative documentation links were checked.
- Runtime/build dependencies installed successfully into a fresh isolated environment from the pinned release list. PyInstaller bundle help, guided-command help and scheduler plan checks passed without creating user data. Both console and windowless launchers were produced; the installer compiled with Inno Setup 6.7.3.
- Real Windows Task Scheduler registration, path/ownership inspection, a help-only windowless launch, and removal were exercised with a temporary task. No email processing or Telegram message was involved.
- A per-user silent test installation into a folder containing spaces and a Chinese character succeeded. Unicode ShellLink targets, Setup/First-run/Troubleshooting/Stop pause arguments and the offline HTML guide were verified. The installed program ran with only Windows System32 on PATH. Setup Exit kept its process waiting until Enter; this checks pause behavior, not visual inspection of a real console window.
- Existing isolated settings and SQLite bytes survived installation and reinstallation unchanged. After a canceled Setup initialization, an existing sentinel SQLite table remained intact. Uninstall removed installed programs/shortcuts and retained the test settings/database byte for byte.

Still pending before public readiness: a clean Windows account with no developer
Python installed; interactive installer/shortcut visual review; actual classic
Outlook/profile and policy-denied cases; live SoCLaaS and Telegram checks;
real timed sync/digest, lock/sleep/sign-out behavior; and live existing-cloud
dataset/webhook acceptance. The GitHub-hosted workflow has not been run here.
The installer is unsigned. These pending checks remain release limitations,
not implied passes from the automated tests or isolated install test.
