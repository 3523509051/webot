@echo off
rem ============================================================
rem  webot - show current status of all services
rem ============================================================
chcp 65001 >nul
title webot - status
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-service.ps1" status
echo.
echo (Press any key to close this window)
pause >nul
