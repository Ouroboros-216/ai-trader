@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install.ps1"
    if errorlevel 1 pause & exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m aitrader.setup_gui
exit /b
