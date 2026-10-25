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

function Resolve-Python {
    foreach ($c in @("$Python", "$env:APPDATA\Programs\Python\Python313\python.exe")) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
    }
    throw "python not found on PATH"
}
$py = Resolve-Python

Write-Host "Registering scheduled task '$Name' -> `"$py -m splitfy.server --port $Port --scope '$repoRoot'"`""
$action  = New-ScheduledTaskAction   -Execute "$py -m splitfy.server" -Argument "--port $Port --scope '$repoRoot'" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger   -At (Get-Date)
$settings= New-ScheduledTaskSettingsSet -Restart 300 -RepeatInterval (TimeSpan::FromMinutes 5)

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings
Write-Host "Done. Start it: schtasks /Query /FO TABLE /V /Filter:""Name=$Name"""
