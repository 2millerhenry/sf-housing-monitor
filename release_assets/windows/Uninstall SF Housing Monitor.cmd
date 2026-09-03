@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0payload\tools\uninstall.ps1"
if errorlevel 1 pause
