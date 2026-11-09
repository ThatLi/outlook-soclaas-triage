[CmdletBinding()]
param([string]$ProjectDir, [string]$TaskPrefix, [switch]$EnableTelegram,
      [switch]$EnableTelegramCommands, [switch]$ShowPlan)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/invoke_scheduler.ps1"
$options = @()
if ($EnableTelegram) { $options += '--telegram' }
if ($EnableTelegramCommands) { $options += '--listener' }
Invoke-TriageScheduler -ProjectDir $ProjectDir -TaskPrefix $TaskPrefix -ShowPlan:$ShowPlan -Options $options
