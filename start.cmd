@echo off
rem ============================================================
rem  webot - one-click START (manual; no scheduled task)
rem  Double-click this file. Also usable after a reboot.
rem
rem  Safe by design:
rem    * does NOT stop or restart what is already running
rem      (a live bridge keeps serving; see restart-bridge.cmd
rem       if a restart is really what you want)
rem    * every process it launches owns a separate hidden
rem      console, so closing this window never kills the bot
rem    * no battery / idle / time-limit conditions anywhere
rem  See docs\deployment.md for details.
rem ============================================================
chcp 65001 >nul
title webot - starting services
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-service.ps1" start
echo.
echo (Press any key to close this window - the bot keeps running)
pause >nul
