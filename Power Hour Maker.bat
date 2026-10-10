@echo off
rem Power Hour Maker launcher. First run sets everything up inside this folder.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: downloading Python, FFmpeg and Deno into this folder. This takes a few minutes...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
    if errorlevel 1 (
        echo.
        echo Setup failed - see the messages above.
        pause
        exit /b 1
    )
)
start "" ".venv\Scripts\pythonw.exe" -m app %*
