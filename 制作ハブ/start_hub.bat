@echo off
cd /d "%~dp0"
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
