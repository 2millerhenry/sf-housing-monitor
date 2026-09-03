@echo off
setlocal
set "SF_HOUSING_DATA_DIR=%LOCALAPPDATA%\SF Housing Monitor\data"
set "SF_HOUSING_STARTUP_PLATFORM=windows"
"%LOCALAPPDATA%\SF Housing Monitor\current\Scripts\python.exe" -m sf_housing serve --host 127.0.0.1 --port 8000
