[CmdletBinding()]
param([int]$Port = 8765, [string]$Python = "")
$ErrorActionPreference = "Stop"
if ($Python) {
    & $Python -m jamly.service install --port $Port @args
} else {
    & jamly-service install --port $Port @args
}
exit $LASTEXITCODE
