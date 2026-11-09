[CmdletBinding()]
param([string]$ProjectDir, [string]$TaskPrefix, [switch]$CachePreviews, [switch]$ShowPlan)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/invoke_scheduler.ps1"
$options = @('--cloud')
if ($CachePreviews) { $options += '--cache-previews' }
Invoke-TriageScheduler -ProjectDir $ProjectDir -TaskPrefix $TaskPrefix -ShowPlan:$ShowPlan -Options $options
