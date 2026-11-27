@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "bridge\.venv\Scripts\python.exe" (
 echo Install bridge/.venv and bridge/requirements.txt first.
 pause
 exit /b 1
)
"bridge\.venv\Scripts\python.exe" "scripts\combo_entry.py" stop %*
set "RESULT=%errorlevel%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%
