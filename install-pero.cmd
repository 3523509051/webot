@echo off
setlocal
chcp 65001 >nul
set "PERO_ROOT=%~dp0..\Pero"
if not "%~1"=="" set "PERO_ROOT=%~1"
if not exist "%PERO_ROOT%\requirements-lock.txt" goto missing
if not exist "%PERO_ROOT%\pyproject.toml" goto missing
if not exist "%PERO_ROOT%\frontend\package-lock.json" goto missing
where node >nul 2>nul
if errorlevel 1 goto missingnode
where npm.cmd >nul 2>nul
if errorlevel 1 goto missingnode
cd /d "%PERO_ROOT%"
if errorlevel 1 goto failed
if not exist ".venv\Scripts\python.exe" (
 python -m venv .venv
 if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --no-deps -e .
if errorlevel 1 goto failed
cd frontend
if errorlevel 1 goto failed
call npm.cmd ci
if errorlevel 1 goto failed
call npm.cmd run build
if errorlevel 1 goto failed
echo Pero backend and control-room frontend are installed. No services were started.
pause
exit /b 0
:missing
echo Pero repository not found or incomplete: "%PERO_ROOT%"
echo Put Pero next to webot, or run install-pero.cmd "D:\Apps\Pero".
pause
exit /b 1
:missingnode
echo Node.js and npm are required. Install Node.js, then reopen this installer.
pause
exit /b 1
:failed
echo Pero installation failed. Check the error above; no services were started.
pause
exit /b 1
