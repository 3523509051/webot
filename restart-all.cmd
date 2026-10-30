@echo off
rem ============================================================
rem  webot - 一键重启：Docker -> AstrBot -> 微信桥（新脚本）
rem  只重启桥：restart-all.cmd bridge
rem  只重启 AstrBot：restart-all.cmd astr
rem  结果写在 logs\restart-last.txt
rem ============================================================
chcp 65001 >nul
title webot - restart
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=all"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\webot-restart.ps1" -Target %TARGET%
echo.
echo (按任意键关闭窗口)
pause >nul
