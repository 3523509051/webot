@echo off
rem ============================================================
rem  webot - SCREEN OFF, keep everything running
rem  Double-click: health check -> switch the display to a VIRTUAL
rem  monitor (usbmmidd_v2). The physical panel loses signal and goes
rem  truly black, while Windows still sees a display attached -> no
rem  connected standby, everything keeps running.
rem  Touch the mouse/keyboard to restore. Double-click again to restore.
rem
rem  This is the ONLY supported way to turn the screen off. Power
rem  settings / DPMS / Modern Standby tricks are NOT used (see
rem  docs\deployment.md section 4.3 "弃用过的三条路").
rem  Needs the driver: scripts\vdd.ps1 -Action setup (once, admin).
rem ============================================================
chcp 65001 >nul
title webot - screen off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\screen-off.ps1"
