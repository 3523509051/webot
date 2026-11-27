@echo off
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure-wechat.ps1" %*
set "RESULT=%errorlevel%"
pause
exit /b %RESULT%
