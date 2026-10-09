@echo off
rem ============================================================
rem  webot - RESTART the WeChat bridge ONLY (AstrBot untouched)
rem  Double-click this file.
rem  Use cases: changed bridge config.json / bridge code, or a
rem  new WeChat group was created (groups are discovered at
rem  startup only). See docs\deployment.md for details.
rem ============================================================
chcp 65001 >nul
title webot - restarting bridge
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-service.ps1" bridge
echo.
echo (Press any key to close this window)
pause >nul
