[CmdletBinding()]
param(
    [string]$WslDistribution = "Ubuntu",
    [string]$WslVenv = "~/.venvs/ai-trader",
    [ValidateSet("run-session", "status", "self-test", "preflight")]
    [string]$Mode = "run-session"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

# A host-local marker provides an immediate, non-administrator kill switch.
# It is deliberately ignored by Git so disabling one runner does not disable
# another checkout, such as the Oracle VM.
$disableMarker = Join-Path $projectRoot ".runtime\disable-local-shadow"
if (Test-Path -LiteralPath $disableMarker) {
    Write-Output "Local SHADOW launcher is disabled by marker: $disableMarker"
    exit 0
}

$logDirectory = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "shadow-session-$stamp.log"

$resolvedRoot = (Resolve-Path -LiteralPath $projectRoot).Path
if ($resolvedRoot -notmatch '^([A-Za-z]):\\(.*)$') {
    throw "Project must be on a Windows drive that WSL can mount: $resolvedRoot"
}
$drive = $Matches[1].ToLowerInvariant()
$relative = $Matches[2] -replace '\\', '/'
$wslProject = "/mnt/$drive/$relative"
$bashCommand = "cd '$wslProject' && source $WslVenv/bin/activate && python orchestrator.py --$Mode"

# Windows PowerShell 5 converts a native program's stderr into non-terminating
# ErrorRecords.  Python/unittest legitimately writes progress to stderr, so a
# global Stop preference would abort the wrapper even when WSL exits with zero.
# Capture both streams in the log and make the native exit code authoritative.
$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    & wsl.exe -d $WslDistribution -- bash -lc $bashCommand 2>&1 | Tee-Object -FilePath $logPath
    $exitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($exitCode -ne 0) {
    Write-Output "Shadow orchestrator failed closed with exit code $exitCode. No live order was possible. Log: $logPath"
}
exit $exitCode
