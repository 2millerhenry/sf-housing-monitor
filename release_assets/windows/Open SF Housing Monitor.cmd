@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0payload\tools\open.ps1"
if errorlevel 1 pause
