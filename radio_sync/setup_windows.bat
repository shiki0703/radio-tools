@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Setup complete. Put ffmpeg.exe and ffprobe.exe in bin.
pause
exit /b 0
:fail
echo Setup failed. Check Python installation and network connection.
pause
exit /b 1
