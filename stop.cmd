@echo off
rem ============================================================
rem  webot - one-click STOP of all services
rem  Stops the WeChat bridge and the AstrBot container.
rem ============================================================
chcp 65001 >nul
title webot - stopping services
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-service.ps1" stop
echo.
echo (Press any key to close this window)
pause >nul
