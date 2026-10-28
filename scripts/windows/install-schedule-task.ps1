#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [ValidateSet("jamly","Jamly")] $Name = "jamly"
)
$ErrorActionPreference = "Stop"

# Walk up from this file until we reach the repo root (the dir containing src\jamly). The wrappers
# now live at <root>/scripts/<os>/<file>, so a depth-independent search keeps this correct.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # .../scripts/windows/
$maxDepth  = 40
while (-not (Test-Path -LiteralPath (Join-Path $scriptDir "src/jamly")) -and $maxDepth -gt 0) {
    $scriptDir = Split-Path -Parent $scriptDir
    $maxDepth--
}
$repoRoot  = $scriptDir   # repo root, so `import jamly` needs no editable install

# NOTE: no hardcoded Python path here — the version and install layout vary per box. Prefer
# `python`/`py` on PATH (covers PATH installs + an editable install); fall back to a couple of
# common locations so it still runs on a fresh machine, then give an actionable error.
function Resolve-Python {
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
$launcher = Join-Path $repoRoot "scripts\windows\start-jamly-server.bat"
$cmdArgs = '/d /c ""{0}" {1}"' -f $launcher, $Port

Write-Host "Registering scheduled task '$Name' -> $launcher $Port"
$action = New-ScheduledTaskAction `
    -Execute $env:ComSpec `
    -Argument $cmdArgs `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Done. Start it: schtasks /Query /FO TABLE /V /Filter:""Name=$Name"""
