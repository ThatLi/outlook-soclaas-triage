[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskPrefix = "Outlook SoCLaaS Triage",
    [switch]$Remove
)

$taskNames = @("$TaskPrefix - Sync", "$TaskPrefix - Digest")
foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "${taskName}: not registered"
        continue
    }
    $usesWsl = @($task.Actions | Where-Object {
        $_.Execute -match '(^|\\)wsl(\.exe)?$'
    }).Count -gt 0
    Write-Host "${taskName}: exists; invokes WSL = $usesWsl"
    if ($Remove -and $usesWsl -and $PSCmdlet.ShouldProcess($taskName, "Unregister legacy WSL scheduled task")) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}

