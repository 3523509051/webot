@echo off
chcp 65001 >nul
cd /d "%~dp0bridge"
if not exist ".venv\Scripts\python.exe" (
  echo Bridge Python is missing. Install bridge requirements first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" bind_account.py
set "binding_exit=%errorlevel%"
pause
exit /b %binding_exit%
