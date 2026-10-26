#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [ValidateSet("splitfy","Splitfy")] $Name = "splitfy"
)
$ErrorActionPreference = "Stop"

# Walk up from this file until we reach the repo root (the dir containing src\splitfy). The wrappers
# now live at <root>/scripts/<os>/<file>, so a depth-independent search keeps this correct.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # .../scripts/windows/
$maxDepth  = 40
while (-not (Test-Path -LiteralPath (Join-Path $scriptDir "src/splitfy")) -and $maxDepth -gt 0) {
    $scriptDir = Split-Path -Parent $scriptDir
    $maxDepth--
}
$repoRoot  = $scriptDir   # repo root, so `import splitfy` needs no editable install

# NOTE: no hardcoded Python path here — the version and install layout vary per box. Prefer
# `python`/`py` on PATH (covers PATH installs + an editable install); fall back to a couple of
# common locations so it still runs on a fresh machine, then give an actionable error.
function Resolve-Python {
    $env:ProgramFiles = "$env:ProgramFiles"
    foreach ($c in @("$Python", "python", "py")) {
        if ($null -ne (Get-Command $c -ErrorAction SilentlyContinue)) { return $c }
    }
    $bases = @(Join-Path "$env:LOCALAPPDATA" "Programs\Python",
               Join-Path "$env:ProgramFiles" "Python")
    foreach ($base in $bases) {
        if (Test-Path -LiteralPath $base) {
            $dirs = Get-ChildItem -LiteralPath $base -Directory | Where-Object { $_.Name -match 'Python\d+' }
            if ($dirs) {
                $exe = Join-Path $dirs[0].FullName "python.exe"
                if (Test-Path -LiteralPath $exe) { return $exe }
            }
        }
    }
    throw "$Python not set and no python on PATH or common Windows locations; set the '$Python' env var."
}
$py = Resolve-Python

Write-Host "Registering scheduled task '$Name' -> `"$py -m splitfy.server --port $Port --scope '$repoRoot'"`""
$action  = New-ScheduledTaskAction   -Execute "$py -m splitfy.server" -Argument "--port $Port --scope '$repoRoot'" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger   -At (Get-Date)
$settings= New-ScheduledTaskSettingsSet -Restart 300 -RepeatInterval (TimeSpan::FromMinutes 5)

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings
Write-Host "Done. Start it: schtasks /Query /FO TABLE /V /Filter:""Name=$Name"""
