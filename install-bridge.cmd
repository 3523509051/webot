@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "bridge\.venv\Scripts\python.exe" (
 python -m venv "bridge\.venv"
 if errorlevel 1 goto failed
)
"bridge\.venv\Scripts\python.exe" -m pip install -r "bridge\requirements.txt"
if errorlevel 1 goto failed
echo Bridge dependencies installed. Configure the WeChat directory, bind the account, then run start.cmd.
pause
exit /b 0
:failed
echo Installation failed. Install 64-bit Python 3.10+ and check the error above.
pause
exit /b 1
