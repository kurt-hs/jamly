[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [ValidateSet("splitfy","Splitfy")] $Name = "splitfy"
)
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Join-Path $scriptDir ".."   # windows/ -> repo root

function Resolve-Python {
    foreach ($c in @("$Python", "$env:APPDATA\Programs\Python\Python313\python.exe")) {
        if (Get-Command $c -ErrorAction SilentlyContinue) { return $c }
    }
    throw "python not found on PATH"
}

# stop any running server instance before unregistering
try { Resolve-Python; & "$py" --version >$null 2>$null } catch {}
Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue }

Write-Host "Unregistering scheduled task '$Name'..."
Unregister-ScheduledTask -TaskName $Name -Confirm:$false
Write-Host "Done."
