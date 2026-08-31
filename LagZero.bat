@echo off
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install it from https://www.python.org/ and enable "Add python.exe to PATH".
    pause
    exit /b 1
)

python main.py %*
exit /b %errorlevel%
