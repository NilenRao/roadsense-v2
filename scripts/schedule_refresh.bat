@echo off
rem Registers a Windows Task Scheduler job that runs the daily data refresh at 06:00.
rem   scripts\schedule_refresh.bat           create or replace the task
rem   scripts\schedule_refresh.bat /delete   remove it
rem Runs as the current user, only when logged on (no stored password). Output: data\live\scheduler.log
setlocal
set "TASK=RoadSense daily refresh"
set "RUNNER=%~dp0run_refresh.bat"
for %%I in ("%RUNNER%") do set "RUNNER=%%~fI"
if /i "%~1"=="/delete" (
  schtasks /Delete /TN "%TASK%" /F
  exit /b %ERRORLEVEL%
)
schtasks /Create /F /SC DAILY /ST 06:00 /TN "%TASK%" /TR "\"%RUNNER%\""
if errorlevel 1 (
  echo Could not create the task. Try running this from a normal ^(non-admin^) command prompt as yourself.
  exit /b 1
)
echo Scheduled "%TASK%" daily at 06:00: %RUNNER%
echo Check it:   schtasks /Query /TN "%TASK%" /V /FO LIST
echo Run it now: schtasks /Run /TN "%TASK%"
