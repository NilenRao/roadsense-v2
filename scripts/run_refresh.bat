@echo off
rem Runs one daily data refresh (python -m roadsense.refresh). Called by the "RoadSense daily refresh"
rem scheduled task; safe to run by hand. Exit code 0 = new build live, 1 = failed (previous data kept).
setlocal
set "REPO=%~dp0.."
for %%I in ("%REPO%") do set "REPO=%%~fI"
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py"
if not exist "%REPO%\data\live" mkdir "%REPO%\data\live"
cd /d "%REPO%"
"%PY%" -m roadsense.refresh %* >> "%REPO%\data\live\scheduler.log" 2>&1
exit /b %ERRORLEVEL%
