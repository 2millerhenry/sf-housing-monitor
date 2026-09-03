[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ReleaseRoot)
$installer = Join-Path $ReleaseRoot 'payload\install.ps1'
if (-not (Test-Path -LiteralPath $installer)) { throw 'Repair needs the extracted SF Housing Monitor folder. Extract the ZIP and double-click Repair there.' }
Write-Host 'Repairing application files. Your profile, listings, and connectors will be preserved.'
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer -ReleaseRoot $ReleaseRoot
exit $LASTEXITCODE
