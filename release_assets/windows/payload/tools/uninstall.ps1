[CmdletBinding()]
param(
  # Removes the app without asking about the data. The macOS uninstaller has
  # always coped with having nobody to ask -- its prompt falls back to keeping
  # the data when there is no one at the keyboard -- and this one blocked
  # forever instead, which makes it impossible to script and impossible to test.
  [switch]$KeepData
)

$ErrorActionPreference = 'Stop'
$TaskName = 'SF Housing Monitor'
$AppRoot = Join-Path $env:LOCALAPPDATA 'SF Housing Monitor'
& schtasks.exe /End /TN $TaskName 2>$null | Out-Null
& schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
foreach ($name in @('current', 'python', 'uv', 'tools', 'releases')) {
  $path = Join-Path $AppRoot $name
  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Write-Host "SF Home Finder was removed. Your private data remains at:`n$AppRoot\data"
if ($KeepData) {
  $confirmation = ''
} else {
  # Nobody at the keyboard is an answer, and the answer is "keep it". Deleting
  # somebody's search history because a script had no stdin would be the worst
  # possible reading of silence.
  try { $confirmation = Read-Host 'Type DELETE to permanently remove private data, or press Enter to keep it' }
  catch { $confirmation = '' }
}
if ($confirmation -eq 'DELETE') {
  foreach ($name in @('data', 'logs', 'backups')) { $path = Join-Path $AppRoot $name; if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force } }
  Write-Host 'Private data was permanently deleted.'
} else { Write-Host 'Private data was preserved. A future install restores it.' }
