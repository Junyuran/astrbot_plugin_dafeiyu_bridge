@echo off
rem ASCII only: cmd.exe reads .cmd as GBK.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
