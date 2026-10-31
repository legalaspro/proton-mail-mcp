<#
.SYNOPSIS
Windows entry point of the proton-mail-mcp installer; Windows is not yet supported.

.DESCRIPTION
Windows is not yet supported: the MCP server keeps its audit, undo and index
state with POSIX file locking (fcntl), so its service cannot start on Windows.
Install, uninstall and show-token therefore stop with exit status 2 before
anything is changed; use WSL2 until native support, planned for v1.1. -Action
detect, and -DryRun -Simulate plans for Linux or macOS, still run the
standard-library installer module proton_mail_mcp.installer from this checkout.
Linux and macOS use scripts/install.sh. See docs/install.md.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Action detect

.EXAMPLE
.\scripts\install.ps1 -DryRun -Simulate debian -BridgeUser you@example.com
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'detect', 'show-token')]
    [string]$Action = 'install',
    [switch]$DryRun,
    [switch]$Yes,
    [string]$BridgeUser,
    [int]$Port,
    [string]$BridgeBin,
    [ValidateSet('debian', 'fedora', 'arch', 'macos', 'windows')]
    [string]$Simulate,
    [switch]$SkipBridge,
    [switch]$SkipCredentials,
    [switch]$SkipServices,
    [switch]$SkipBridgeService,
    [switch]$ReplaceCredentials,
    [switch]$RemoveCredentials,
    [switch]$RemoveBridge,
    [switch]$Force,
    [switch]$Json,
    [string]$Python
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
# Same text as NOT_YET_SUPPORTED in proton_mail_mcp/installer/detect.py.
$refusal = 'Windows is not yet supported: the MCP server keeps its audit, undo and index ' +
    'state with POSIX file locking (fcntl), so its service cannot start on Windows. ' +
    'Use WSL2 (run `wsl --install`, then run this command inside Linux); native ' +
    'Windows support is planned for v1.1. See docs/install.md.'
if ($Action -ne 'detect' -and (-not $Simulate -or $Simulate -eq 'windows')) {
    # Refuse before looking for Python, so nobody installs it for nothing.
    [Console]::Error.WriteLine("install.ps1: $refusal")
    exit 2
}

function Test-Python([string[]]$Command) {
    $exe = $Command[0]
    $prefix = @($Command | Select-Object -Skip 1)
    try {
        & $exe @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null |
            Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Find-Python {
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    if ($env:PMM_PYTHON) { $candidates += , @($env:PMM_PYTHON) }
    foreach ($version in '3.14', '3.13', '3.12') { $candidates += , @('py', "-$version") }
    $candidates += , @('python')
    $candidates += , @('python3')
    foreach ($candidate in $candidates) {
        # The Microsoft Store alias for python.exe fails this check and is skipped.
        if ((Get-Command $candidate[0] -ErrorAction SilentlyContinue) -and (Test-Python $candidate)) {
            return , $candidate
        }
    }
    return $null
}

$found = Find-Python
if (-not $found) {
    Write-Host 'install.ps1: Python 3.12 or newer is required. Install it, for example:'
    Write-Host '  winget install --id Python.Python.3.12 --exact --source winget'
    Write-Host 'then open a new PowerShell window and rerun this script (or pass -Python).'
    exit 3
}

$moduleArgs = @($Action)
if ($Action -eq 'detect') {
    if ($Json) { $moduleArgs += '--json' }
    if ($Simulate) { $moduleArgs += @('--simulate', $Simulate) }
}
elseif ($Action -eq 'show-token') {
    if ($BridgeUser) { $moduleArgs += @('--bridge-user', $BridgeUser) }
}
else {
    if ($DryRun) { $moduleArgs += '--dry-run' }
    if ($Yes) { $moduleArgs += '--yes' }
    if ($Force) { $moduleArgs += '--force' }
    if ($Simulate) { $moduleArgs += @('--simulate', $Simulate) }
    if ($BridgeUser) { $moduleArgs += @('--bridge-user', $BridgeUser) }
    if ($Action -eq 'install') {
        if ($PSBoundParameters.ContainsKey('Port')) { $moduleArgs += @('--port', "$Port") }
        if ($BridgeBin) { $moduleArgs += @('--bridge-bin', $BridgeBin) }
        if ($SkipBridge) { $moduleArgs += '--skip-bridge' }
        if ($SkipCredentials) { $moduleArgs += '--skip-credentials' }
        if ($SkipServices) { $moduleArgs += '--skip-services' }
        if ($SkipBridgeService) { $moduleArgs += '--skip-bridge-service' }
        if ($ReplaceCredentials) { $moduleArgs += '--replace-credentials' }
    }
    else {
        if ($RemoveCredentials) { $moduleArgs += '--remove-credentials' }
        if ($RemoveBridge) { $moduleArgs += '--remove-bridge' }
    }
}

$saved = @{ PYTHONPATH = $env:PYTHONPATH; PYTHONDONTWRITEBYTECODE = $env:PYTHONDONTWRITEBYTECODE }
$code = 1
try {
    # -P keeps the current directory off sys.path and -s ignores user site-packages,
    # so only this checkout's src provides the installer.
    $env:PYTHONPATH = Join-Path $root 'src'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $exe = $found[0]
    $prefix = @($found | Select-Object -Skip 1)
    & $exe @prefix -P -s -m proton_mail_mcp.installer @moduleArgs
    $code = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $saved.PYTHONPATH
    $env:PYTHONDONTWRITEBYTECODE = $saved.PYTHONDONTWRITEBYTECODE
}
exit $code
