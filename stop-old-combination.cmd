@echo off
setlocal
cd /d "%~dp0"
if not exist "bridge\.venv\Scripts\python.exe" exit /b 1
"bridge\.venv\Scripts\python.exe" "scripts\stop_old_combination.py" %*
pause
