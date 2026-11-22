@echo off
REM One-command Lenovo queue/worker screen: auto-refreshing read-only view.
REM No secrets here: the config and the key paths live outside this repo
REM (see docs/lenovo-queue-screen.md). Read-only: never stops, restarts,
REM retries or recovers anything. Extra flags: run queue_screen.py directly.
setlocal
set "CONFIG=%LOCALAPPDATA%\task-runner\lenovo-worker.json"
if not "%~1" == "" set "CONFIG=%~1"
python "%~dp0queue_screen.py" --config "%CONFIG%"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
