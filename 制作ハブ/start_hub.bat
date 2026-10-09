@echo off
rem Start the hub the same way as the top-level launcher (checks for a new version first).
cd /d "%~dp0"
if exist "%~dp0..\launcher\start.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\launcher\start.ps1"
  if errorlevel 1 pause
  exit /b
)
rem Standalone hub (no launcher folder): start with the Python on this PC.
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 hub.py
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw hub.py
  exit /b 0
)
echo Python not found. Install Python from https://www.python.org/downloads/
pause
exit /b 1
