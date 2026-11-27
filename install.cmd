@echo off
setlocal
call "%~dp0install-pero.cmd" %*
if errorlevel 1 exit /b 1
call "%~dp0install-bridge.cmd"
if errorlevel 1 exit /b 1
echo Installation complete. Configure the WeChat directory, bind the account, then run start.cmd.
pause
exit /b 0
