function Invoke-TriageScheduler {
    param([string]$ProjectDir, [string]$TaskPrefix, [switch]$ShowPlan, [string[]]$Options)
    if ([string]::IsNullOrWhiteSpace($ProjectDir)) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
    $project = (Resolve-Path -LiteralPath $ProjectDir).Path
    $program = Join-Path $project 'outlook-triage.exe'
    $leading = @()
    if (-not (Test-Path -LiteralPath $program -PathType Leaf)) {
        $program = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv/Scripts/python.exe'
        $leading = @('-m', 'outlook_triage')
    }
    if (-not (Test-Path -LiteralPath $program -PathType Leaf)) {
        throw 'Outlook Triage is missing. Build/install the release, or create the development virtual environment first.'
    }
    $action = if ($ShowPlan) { 'plan' } else { 'enable' }
    $arguments = $leading + @('background', $action, '--project-dir', $project) + $Options
    if ($TaskPrefix) { $arguments += @('--task-prefix', $TaskPrefix) }
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Background registration could not finish. Run Outlook Triage Troubleshooting, correct the reported problem, and retry.' }
}
