$ErrorActionPreference = 'Stop'
$tool = Join-Path $env:LOCALAPPDATA 'SF Home Finder\tools\open.ps1'
if (-not (Test-Path -LiteralPath $tool)) { throw 'SF Home Finder is not installed. Double-click Install first.' }
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $tool
$report = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/support/report.json' -TimeoutSec 5
if ($null -eq $report.overall) { throw 'The local dashboard started, but its Ready Check did not respond. Double-click Repair.' }
Start-Process 'http://127.0.0.1:8000/support'
Write-Host 'Ready check completed. Opening the private support page.'
