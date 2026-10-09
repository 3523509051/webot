@echo off
rem ============================================================
rem  webot - one-click START (or RESTART) of all services
rem  Double-click this file. Also usable after a reboot.
rem  See docs\deployment.md for details.
rem ============================================================
chcp 65001 >nul
title webot - starting services
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-service.ps1" start
echo.
echo (Press any key to close this window)
pause >nul
