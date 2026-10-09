@echo off
rem ============================================================
rem  webot - RESTART AstrBot ONLY (bridge keeps running)
rem  Double-click this file.
rem  Use cases: persona edited outside the WebUI, main config
rem  (cmd_config.json) changed, or plugin code updated.
rem  The bridge reconnects automatically - no action needed.
rem ============================================================
chcp 65001 >nul
title webot - restarting AstrBot
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-service.ps1" astr
echo.
echo (Press any key to close this window)
pause >nul
