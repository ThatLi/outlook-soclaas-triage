# Outlook–SoCLaaS Triage

Turn selected classic Outlook Inbox messages into a Telegram task brief using
SoCLaaS. Local Windows operation is the recommended first-release path.

Download [Outlook-SoCLaaS-Triage-1.0.0-windows-x64-setup.exe](https://github.com/ThatLi/outlook-soclaas-triage/releases/download/v1.0.0/Outlook-SoCLaaS-Triage-1.0.0-windows-x64-setup.exe). Python is bundled.
Install for your Windows account, then open **Outlook Triage - Setup** from Start.
Complete **Outlook Triage - First-run Check** before enabling background processing.

Read the [installation and daily-use guide](docs/User-Guide.md) and
[symptom-based troubleshooting](docs/Troubleshooting.md) before processing email.
You need Windows 10/11 x64, classic Outlook, SoCLaaS access, a Telegram account,
and your organization's permission to send email text to SoCLaaS and summaries
or requested previews to Telegram. New Outlook is unsupported.

The first release is unsigned unless a release publisher explicitly provides a
signed artifact. Do not disable Windows protections to install it. Release
validation and remaining manual checks are described in [Release](docs/Release.md).

Advanced use: [source installation](docs/Source-Installation.md),
[full command-line reference](docs/Advanced-CLI.md),
[existing cloud bot/deployment](docs/Cloud-Bot.md), and
[legacy outbound relay](docs/Cloud-Relay.md).
There is no hosted service, automatic updater, or Windows service.

Maintainers: build with Windows PowerShell 5.1 or PowerShell 7 using `./scripts/build_release.ps1` on Windows.
The [release guide](docs/Release.md) lists prerequisites, pinned tooling, artifact
verification and the manual acceptance checklist. The Windows release workflow
produces an installer and SHA-256 checksum; publishing a public Release is a
separate maintainer action after acceptance.
