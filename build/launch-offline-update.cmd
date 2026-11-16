@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 31
if exist payload exit /b 31
if not exist manifest.json exit /b 31
if not exist app.asar exit /b 31
if not exist home-study-local-agent.exe exit /b 31
if not exist hsc-offline-update-helper.exe exit /b 31
mkdir payload
if errorlevel 1 exit /b 31
move "manifest.json" "payload\manifest.json" >nul
if errorlevel 1 exit /b 31
move "app.asar" "payload\app.asar" >nul
if errorlevel 1 exit /b 31
move "home-study-local-agent.exe" "payload\home-study-local-agent.exe" >nul
if errorlevel 1 exit /b 31
"hsc-offline-update-helper.exe" --manifest "%~dp0payload\manifest.json" --payload-dir "%~dp0payload" --interactive
set "HSC_OFFLINE_UPDATE_EXIT=%ERRORLEVEL%"
endlocal & exit /b %HSC_OFFLINE_UPDATE_EXIT%
