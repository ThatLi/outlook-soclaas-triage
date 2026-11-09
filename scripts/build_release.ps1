#Requires -Version 5.1
[CmdletBinding()]
param([string]$Python = 'python', [string]$ISCC, [switch]$UseExistingEnvironment)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows release on Windows x64.' }
if (-not $ISCC) {
    $ISCC = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
    if (-not $ISCC) { $ISCC = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe' }
}
if (-not (Test-Path -LiteralPath $ISCC -PathType Leaf)) { throw 'Install Inno Setup 6.7.3 or pass -ISCC with its compiler path.' }
function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Release build stopped because a build tool failed. Review the output above; no release should be published.' }
}
Invoke-Checked $Python @('-c', 'import sys; assert sys.version_info[:3] == (3, 14, 2) and sys.maxsize > 2**32')
if (-not $UseExistingEnvironment) {
    Invoke-Checked $Python @('-m', 'venv', 'build/release-venv')
    $Python = Join-Path $root 'build/release-venv/Scripts/python.exe'
    Invoke-Checked $Python @('-m', 'pip', 'install', '-r', 'packaging/requirements-release.txt')
    Invoke-Checked $Python @('-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '.')
}
Invoke-Checked $Python @('-m', 'pip', 'check')
$version = & $Python -c 'from outlook_triage import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid application version.' }
$projectVersion = [regex]::Match((Get-Content -LiteralPath 'pyproject.toml' -Raw), '(?m)^version = "([^"]+)"').Groups[1].Value
if ($version -ne $projectVersion) { throw 'Package and application versions differ.' }
Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean', 'packaging/outlook-triage.spec')
Invoke-Checked $Python @('scripts/render_guides.py')
Invoke-Checked $Python @('scripts/verify_bundle.py', 'dist/OutlookTriage')
Invoke-Checked $ISCC @("/DAppVersion=$version", 'packaging/installer.iss')
$artifact = "dist/Outlook-SoCLaaS-Triage-$version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $artifact)) { throw 'Installer compiler did not produce the expected file.' }
(Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLower() + '  ' + (Split-Path -Leaf $artifact) |
    Set-Content -LiteralPath "$artifact.sha256" -Encoding ascii
Write-Host "Built $artifact (unsigned unless separately signed)."
