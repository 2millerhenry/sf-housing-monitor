$ErrorActionPreference = 'Stop'
$TaskName = 'SF Housing Monitor'
$AppRoot = Join-Path $env:LOCALAPPDATA 'SF Housing Monitor'
& schtasks.exe /End /TN $TaskName 2>$null | Out-Null
& schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
foreach ($name in @('current', 'python', 'uv', 'tools', 'releases')) {
  $path = Join-Path $AppRoot $name
  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Write-Host "SF Housing Monitor was removed. Your private data remains at:`n$AppRoot\data"
$confirmation = Read-Host 'Type DELETE to permanently remove private data, or press Enter to keep it'
if ($confirmation -eq 'DELETE') {
  foreach ($name in @('data', 'logs', 'backups')) { $path = Join-Path $AppRoot $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force } }
  Write-Host 'Private data was permanently deleted.'
} else { Write-Host 'Private data was preserved. A future install restores it.' }
