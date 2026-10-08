function Test-OutlookTriageWslAction {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Execute
    )

    if ([string]::IsNullOrWhiteSpace($Execute)) {
        return $false
    }
    $trimmed = $Execute.Trim().Trim('"')
    $fileName = [System.IO.Path]::GetFileName($trimmed)
    return $fileName -imatch '^wsl(?:\.exe)?$'
}

function Get-OutlookTriageReplacementDecision {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [string[]]$ExistingActionExecutables = @(),
        [switch]$ReplaceLegacyWslTasks
    )

    $actions = @($ExistingActionExecutables | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $usesWsl = @($actions | Where-Object { Test-OutlookTriageWslAction -Execute $_ }).Count -gt 0
    $allowed = -not $usesWsl -or [bool]$ReplaceLegacyWslTasks
    $reason = if (-not $usesWsl) {
        "no-legacy-wsl-action"
    } elseif ($ReplaceLegacyWslTasks) {
        "legacy-wsl-replacement-authorized"
    } else {
        "legacy-wsl-replacement-required"
    }

    [pscustomobject]@{
        allowed = $allowed
        usesWsl = $usesWsl
        reason = $reason
    }
}

Export-ModuleMember -Function Test-OutlookTriageWslAction, Get-OutlookTriageReplacementDecision
