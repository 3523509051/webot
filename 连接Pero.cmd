@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "bridge\.venv\Scripts\python.exe" (
 echo Run install-bridge.cmd first.
 pause
 exit /b 1
)
"bridge\.venv\Scripts\python.exe" "scripts\connect_pero.py" %*
set "RESULT=%errorlevel%"
pause
exit /b %RESULT%
