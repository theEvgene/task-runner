@echo off
REM One-click Lenovo launcher: connects the existing SMB share and starts
REM the worker + cleaner. No secrets here: the config and the password file
REM live outside this repo (see docs/lenovo-worker-shortcut.md).
setlocal
set "CONFIG=%LOCALAPPDATA%\task-runner\lenovo-worker.json"
if not "%~1" == "" set "CONFIG=%~1"
python "%~dp0launch.py" --config "%CONFIG%"
endlocal
