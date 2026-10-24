[CmdletBinding()]
param(
    [string]$ProjectDir,
    [string]$TaskPrefix = "Outlook SoCLaaS Triage",
    [switch]$CachePreviews,
    [switch]$ShowPlan
)
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectDir)) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
$project = (Resolve-Path -LiteralPath $ProjectDir).Path
$legacyExecutable = Join-Path $project ".venv\Scripts\outlook-triage.exe"
# pythonw keeps this persistent background bridge from opening a console window.
$executable = Join-Path $project ".venv\Scripts\pythonw.exe"
$arguments = if ($CachePreviews) { "-m outlook_triage bridge-listen --cache-previews" } else { "-m outlook_triage bridge-listen" }
$legacyNames = @("$TaskPrefix - Sync", "$TaskPrefix - Digest", "$TaskPrefix - Telegram Commands")
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($ShowPlan) {
    [ordered]@{
        executable = $executable; arguments = $arguments; workingDirectory = $project
        taskName = "$TaskPrefix - Cloud Bridge"; trigger = "AtLogOn"; startNow = $true
        syncTimesSingapore = @("00:00", "04:00", "08:00", "12:00", "16:00", "20:00")
        pollIntervalSeconds = 30; stopAndDisable = $legacyNames; user = $user
    } | ConvertTo-Json -Depth 4
    return
}
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Missing Windows virtual environment: $executable" }
$preflightPython = Join-Path $project ".venv\Scripts\python.exe"
& $preflightPython -m outlook_triage cloud-check
if ($LASTEXITCODE -ne 0) { throw "Cloud bot is unreachable or misconfigured; existing schedules were not changed." }
# Validate every existing task before changing any of them. A prefix collision must
# not disable another project's tasks.
$legacyTasks = @()
foreach ($name in $legacyNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        foreach ($action in $task.Actions) {
            if ($action.Execute -ne $legacyExecutable -or $action.WorkingDirectory -ne $project) {
                throw "Task '$name' belongs to a different installation; use its own TaskPrefix."
            }
        }
        $legacyTasks += $task
    }
}
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "$TaskPrefix - Cloud Bridge" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "Outbound cloud command bridge; Outlook sync every four hours in Singapore time." -Force | Out-Null
foreach ($task in $legacyTasks) {
    Stop-ScheduledTask -InputObject $task
    Disable-ScheduledTask -InputObject $task | Out-Null
}
Start-ScheduledTask -TaskName "$TaskPrefix - Cloud Bridge"
Write-Host "Cloud bridge started and registered at logon. Legacy schedules are disabled, not deleted."
