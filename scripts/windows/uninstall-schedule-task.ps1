[CmdletBinding()]
param([ValidateSet("jamly","Jamly")] $Name = "jamly")
$ErrorActionPreference = "Stop"

# stop any running server instance before unregistering
Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue }

Write-Host "Unregistering scheduled task '$Name'..."
Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Done."
