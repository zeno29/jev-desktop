@echo off
rem Double-click to install or update Jev. Options are passed through, e.g.:  install.bat -Startup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
