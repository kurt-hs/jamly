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

# stop any running server instance before unregistering
try { Resolve-Python; & "$py" --version >$null 2>$null } catch {}
Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue }

Write-Host "Unregistering scheduled task '$Name'..."
Unregister-ScheduledTask -TaskName $Name -Confirm:$false
Write-Host "Done."
