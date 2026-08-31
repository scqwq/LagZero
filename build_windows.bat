@echo off
setlocal enabledelayedexpansion

echo.
echo  ================================================
echo   LagZero v1.0 - Windows Build Script
echo  ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo         Install it from https://www.python.org/ and enable "Add python.exe to PATH".
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^1') do echo [OK] Found %%v

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] PyInstaller is not installed yet; it will be installed from requirements.txt.
) else (
    for /f "tokens=*" %%v in ('python -m PyInstaller --version 2^>^1') do echo [OK] PyInstaller %%v
)

echo.
echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo       Done.
echo.

echo [2/3] Building LagZero.exe...
echo       This can take 1-3 minutes.
echo.
python -m PyInstaller LagZero.spec --noconfirm --clean
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed.
    echo         Check the messages above for the missing dependency or antivirus block.
    pause
    exit /b 1
)

if not exist "dist\LagZero.exe" (
    echo [ERROR] dist\LagZero.exe was not created.
    pause
    exit /b 1
)

for %%F in ("dist\LagZero.exe") do (
    set /a MB=%%~zF / 1048576
    echo [OK] Portable build: dist\LagZero.exe
    echo     Size: %%~zF bytes ^(!MB! MB^)
)

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

echo.
echo [3/3] Preparing distribution...
if exist "%ISCC%" (
    "%ISCC%" "installer\LagZero.iss"
    if errorlevel 1 (
        echo [ERROR] Inno Setup installer build failed.
        pause
        exit /b 1
    )
    echo [OK] Installer: dist\LagZero-1.0.0-setup.exe
) else (
    echo [INFO] Inno Setup 6 was not found; built the portable executable only.
    echo       Install Inno Setup 6 to also produce the setup executable.
)

echo.
echo Build complete.
echo.
pause
