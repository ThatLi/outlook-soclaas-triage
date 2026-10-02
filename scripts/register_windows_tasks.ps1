[CmdletBinding()]
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskPrefix = "Outlook SoCLaaS Triage",
    [switch]$ReplaceLegacyWslTasks
)

$ErrorActionPreference = "Stop"
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
$digestAction = New-ScheduledTaskAction -Execute $executable -Argument "digest" -WorkingDirectory $project

$syncTrigger = New-ScheduledTaskTrigger -Daily -At "03:50"
$syncTrigger.Repetition.Interval = "PT4H"
$syncTrigger.Repetition.Duration = "P1D"
$digestTrigger = New-ScheduledTaskTrigger -Daily -At "08:00"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "$TaskPrefix - Sync" -Action $syncAction -Trigger $syncTrigger `
    -Settings $settings -Principal $principal `
    -Description "Read-only classic Outlook synchronization and SoCLaaS classification every four hours." `
    -Force | Out-Null

Register-ScheduledTask -TaskName "$TaskPrefix - Digest" -Action $digestAction -Trigger $digestTrigger `
    -Settings $settings -Principal $principal `
    -Description "Generate the local Outlook triage digest daily at 08:00." `
    -Force | Out-Null

Write-Host "Registered native Windows tasks for $currentUser."
Write-Host "They run only while that user is logged in."

