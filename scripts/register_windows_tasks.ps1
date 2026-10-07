[CmdletBinding()]
param(
    [string]$ProjectDir,
    [string]$TaskPrefix = "Outlook SoCLaaS Triage",
    [switch]$ReplaceLegacyWslTasks,
    [switch]$EnableTelegram
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $scriptPath = $PSCommandPath
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        $scriptPath = $MyInvocation.MyCommand.Path
    }
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw "Unable to determine the scheduler script location. Re-run with -ProjectDir <project-path>."
    }
    $scriptDirectory = Split-Path -Parent $scriptPath
    $ProjectDir = Split-Path -Parent $scriptDirectory
}

$project = (Resolve-Path -LiteralPath $ProjectDir).Path
$executable = Join-Path $project ".venv\Scripts\outlook-triage.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "The Windows virtual environment is missing: $executable"
}

$taskNames = @("$TaskPrefix - Sync", "$TaskPrefix - Digest")
foreach ($taskName in $taskNames) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        $usesWsl = @($existing.Actions | Where-Object {
            $_.Execute -match '(^|\\)wsl(\.exe)?$'
        }).Count -gt 0
        if ($usesWsl -and -not $ReplaceLegacyWslTasks) {
            throw "'$taskName' still invokes WSL. Re-run with -ReplaceLegacyWslTasks after reviewing the legacy task."
        }
    }
}

$syncAction = New-ScheduledTaskAction -Execute $executable -Argument "sync" -WorkingDirectory $project
$digestArguments = if ($EnableTelegram) { "digest --telegram" } else { "digest" }
$digestAction = New-ScheduledTaskAction -Execute $executable -Argument $digestArguments -WorkingDirectory $project

$syncTriggers = @(
    "03:50",
    "07:50",
    "11:50",
    "15:50",
    "19:50",
    "23:50"
) | ForEach-Object {
    New-ScheduledTaskTrigger -Daily -At $_
}

$digestTrigger = New-ScheduledTaskTrigger -Daily -At "08:00"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "$TaskPrefix - Sync" -Action $syncAction -Trigger $syncTriggers `
    -Settings $settings -Principal $principal `
    -Description "Read-only classic Outlook synchronization and SoCLaaS classification every four hours." `
    -Force | Out-Null

Register-ScheduledTask -TaskName "$TaskPrefix - Digest" -Action $digestAction -Trigger $digestTrigger `
    -Settings $settings -Principal $principal `
    -Description $(if ($EnableTelegram) { "Generate the local Outlook triage digest and deliver it to Telegram daily at 08:00." } else { "Generate the local Outlook triage digest daily at 08:00." }) `
    -Force | Out-Null

Write-Host "Registered native Windows tasks for $currentUser."
Write-Host "They run only while that user is logged in."
