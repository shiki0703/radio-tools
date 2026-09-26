@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\start.ps1"
if not "%errorlevel%"=="0" pause
