@echo off
setlocal
set "PERO_ROOT=%~dp0..\Pero"
if not "%~1"=="" set "PERO_ROOT=%~1"
if not exist "%PERO_ROOT%\install-pero.cmd" (
 echo Pero installer not found. Update both repositories and place them next to each other.
 echo For another location run install.cmd "D:\Apps\Pero".
 pause
 exit /b 1
)
call "%PERO_ROOT%\install-pero.cmd"
if errorlevel 1 exit /b 1
call "%~dp0install-bridge.cmd"
if errorlevel 1 exit /b 1
echo Installation complete. Configure the WeChat directory, bind the account, then run start.cmd.
pause
exit /b 0
