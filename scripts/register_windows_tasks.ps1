[CmdletBinding()]
param(
    [string]$ProjectDir,
    [string]$TaskPrefix = "Outlook SoCLaaS Triage",
    [switch]$ReplaceLegacyWslTasks,
    [switch]$EnableTelegram,
    [switch]$ShowPlan
)

$ErrorActionPreference = "Stop"

$scriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($scriptPath)) {
    $scriptPath = $MyInvocation.MyCommand.Path
}
if ([string]::IsNullOrWhiteSpace($scriptPath)) {
    throw "Unable to determine the scheduler script location. Re-run with -ProjectDir <project-path>."
}
$scriptDirectory = Split-Path -Parent $scriptPath
Import-Module (Join-Path $scriptDirectory "OutlookTriage.Scheduler.psm1") -Force

if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $ProjectDir = Split-Path -Parent $scriptDirectory
}

$project = (Resolve-Path -LiteralPath $ProjectDir).Path
$executable = Join-Path $project ".venv\Scripts\outlook-triage.exe"

$syncTimes = @("03:50", "07:50", "11:50", "15:50", "19:50", "23:50")
$digestArguments = if ($EnableTelegram) { "digest --telegram" } else { "digest" }
$syncDescription = "Read-only classic Outlook synchronization and SoCLaaS classification every four hours."
$digestDescription = if ($EnableTelegram) {
    "Generate the local Outlook triage digest and deliver it to Telegram daily at 08:00."
} else {
    "Generate the local Outlook triage digest daily at 08:00."
}
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$plan = [ordered]@{
    projectDir = $project
    executable = $executable
    telegramEnabled = [bool]$EnableTelegram
    settings = [ordered]@{
        startWhenAvailable = $true
        allowStartOnBatteries = $true
        stopIfGoingOnBatteries = $false
        wakeToRun = $false
        multipleInstances = "IgnoreNew"
        executionTimeLimit = "PT2H"
    }
    principal = [ordered]@{
        userId = $currentUser
        logonType = "Interactive"
        runLevel = "Limited"
    }
    tasks = @(
        [ordered]@{
            name = "$TaskPrefix - Sync"
            arguments = "sync"
            workingDirectory = $project
            triggerTimes = $syncTimes
            description = $syncDescription
        },
        [ordered]@{
            name = "$TaskPrefix - Digest"
            arguments = $digestArguments
            workingDirectory = $project
            triggerTimes = @("08:00")
            description = $digestDescription
            restartCount = $(if ($EnableTelegram) { 47 } else { 0 })
            restartInterval = $(if ($EnableTelegram) { "PT30M" } else { $null })
        }
    )
}

if ($ShowPlan) {
    $plan | ConvertTo-Json -Depth 6
    return
}

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "The Windows virtual environment is missing: $executable"
}

$taskNames = @($plan.tasks | ForEach-Object { $_.name })
foreach ($taskName in $taskNames) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $existingExecutables = if ($existing) { @($existing.Actions | ForEach-Object { $_.Execute }) } else { @() }
    $decision = Get-OutlookTriageReplacementDecision `
        -ExistingActionExecutables $existingExecutables `
        -ReplaceLegacyWslTasks:$ReplaceLegacyWslTasks
    if (-not $decision.allowed) {
        throw "'$taskName' still invokes WSL. Re-run with -ReplaceLegacyWslTasks after reviewing the legacy task."
    }
}

$syncAction = New-ScheduledTaskAction -Execute $executable -Argument "sync" -WorkingDirectory $project
$digestAction = New-ScheduledTaskAction -Execute $executable -Argument $digestArguments -WorkingDirectory $project

$syncTriggers = $syncTimes | ForEach-Object {
    New-ScheduledTaskTrigger -Daily -At $_
}

$digestTrigger = New-ScheduledTaskTrigger -Daily -At "08:00"

$syncSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$digestSettings = if ($EnableTelegram) {
    New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -RestartCount 47 `
        -RestartInterval (New-TimeSpan -Minutes 30)
} else {
    New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
}

$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "$TaskPrefix - Sync" -Action $syncAction -Trigger $syncTriggers `
    -Settings $syncSettings -Principal $principal `
    -Description $syncDescription `
    -Force | Out-Null

Register-ScheduledTask -TaskName "$TaskPrefix - Digest" -Action $digestAction -Trigger $digestTrigger `
    -Settings $digestSettings -Principal $principal `
    -Description $digestDescription `
    -Force | Out-Null

Write-Host "Registered native Windows tasks for $currentUser."
Write-Host "They run only while that user is logged in."
