@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "bridge\.venv\Scripts\python.exe" (
 echo Run install-bridge.cmd first to create bridge/.venv and install requirements.
 pause
 exit /b 1
)
"bridge\.venv\Scripts\python.exe" "scripts\combo_entry.py" status %*
set "RESULT=%errorlevel%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%
