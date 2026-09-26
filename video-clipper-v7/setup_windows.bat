@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo Setup complete, but FFmpeg was not found.
  echo Run "winget install Gyan.FFmpeg" in PowerShell, then restart the PC.
  pause
  exit /b 0
)
echo Setup complete. Start the app with start_windows.bat.
pause
exit /b 0
:fail
echo Setup failed. Check Python installation and network connection.
pause
exit /b 1
