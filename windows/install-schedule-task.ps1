#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [ValidateSet("splitfy","Splitfy")] $Name = "splitfy"
)
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Join-Path $scriptDir ".."   # windows/ -> repo root, so `import splitfy` works

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
