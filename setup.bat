@echo off
setlocal enabledelayedexpansion
::
:: SyncGuard Installer — Clone the repo and install dependencies.
:: Usage:
::   setup.bat                        Install to default location (C:\SyncGuard)
::   setup.bat C:\MyFolder\SyncGuard  Install to a custom location
::

:: ── Default install location ────────────────────────────────────
set "INSTALL_DIR=%~1"
if not defined INSTALL_DIR set "INSTALL_DIR=C:\SyncGuard"

echo ========================================
echo SyncGuard Setup
echo ========================================
echo.
echo Install location: %INSTALL_DIR%
echo.

:: ── Check for Git ──────────────────────────────────────────────
where git >nul 2>&1
if errorlevel 1 (
    echo [X] Git is not installed or not in PATH.
    echo.
    echo Download it from: https://git-scm.com/download/win
    echo During install, select "Add to PATH".
    exit /b 1
)
echo [i] Git found: OK

:: ── Check for Python ───────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    where python3 >nul 2>&1
    if errorlevel 1 (
        echo [X] Python is not installed or not in PATH.
        echo.
        echo Download it from: https://www.python.org/downloads/
        echo During install, check "Add Python to PATH".
        exit /b 1
    )
    set "PYTHON=python3"
) else (
    set "PYTHON=python"
)
echo [i] Python found: OK

:: Show Python version
for /f "tokens=*" %%v in ('%PYTHON% --version 2^>^&1') do set "PYVER=%%v"
echo [i] %PYVER%

:: Locate pythonw.exe (suppresses console window)
set "PYTHONW=%PYTHON%"
for /f "tokens=*" %%w in ('where pythonw 2^>nul') do (
    if not defined PYTHONW set "PYTHONW=%%w"
)
if "%PYTHONW%"=="%PYTHON%" (
    set "PYTHONW=%PYTHON:python=pythonw%"
)
echo.

:: ── Check if folder already has a git repo ─────────────────────
if exist "%INSTALL_DIR%\.git" (
    echo [!] %INSTALL_DIR% already contains a SyncGuard git repository.
    echo.
    set /p "CHOICE= Run update instead? (Y/N): "
    if /i "!CHOICE!"=="Y" (
        cd /d "%INSTALL_DIR%"
        echo.
        echo [v] Pulling latest changes...
        git pull origin main
        if errorlevel 1 (
            echo [X] Update failed. Check for local conflicts.
            exit /b 1
        )
        goto :install_deps
    )
    echo [i] Skipping clone.
    goto :install_deps
)

:: ── Folder exists but is not a git repo ────────────────────────
if exist "%INSTALL_DIR%" (
    echo [!] %INSTALL_DIR% exists but is not a git repository.
    echo.
    set /p "CHOICE= Initialize git and pull SyncGuard into it? (Y/N): "
    if /i "!CHOICE!"=="Y" (
        cd /d "%INSTALL_DIR%"
        git init
        git remote add origin https://github.com/HempsSA/SyncGuard.git
        git fetch origin
        git checkout -b main origin/main
        goto :install_deps
    )
    echo [i] Skipping. Installing dependencies only.
    goto :install_deps
)

:: ── Fresh install — clone into a temp folder then move ─────────
echo [v] Cloning SyncGuard repository...
set "TEMP_CLONE=%INSTALL_DIR%_clone_%RANDOM%"
git clone https://github.com/HempsSA/SyncGuard.git "%TEMP_CLONE%"
if errorlevel 1 (
    echo [X] Clone failed. Check your internet connection.
    rmdir /s /q "%TEMP_CLONE%" 2>nul
    exit /b 1
)

:: Move contents from temp clone into the target directory
mkdir "%INSTALL_DIR%" 2>nul
xcopy "%TEMP_CLONE%\*" "%INSTALL_DIR%\" /E /Y /Q >nul
if errorlevel 1 (
    echo [X] Failed to move files into %INSTALL_DIR%.
    rmdir /s /q "%TEMP_CLONE%" 2>nul
    exit /b 1
)
rmdir /s /q "%TEMP_CLONE%" 2>nul
echo.

:install_deps
:: ── Install Python dependencies ────────────────────────────────
echo [v] Installing Python dependencies...
%PYTHON% -m pip install --upgrade pip --quiet
%PYTHON% -m pip install -r "%INSTALL_DIR%\requirements.txt"
if errorlevel 1 (
    echo.
    echo [X] Failed to install dependencies.
    echo Try running manually: %PYTHON% -m pip install -r "%INSTALL_DIR%\requirements.txt"
    exit /b 1
)
echo.

:: ── Done ───────────────────────────────────────────────────────
echo ========================================
echo Setup complete!
echo ========================================
echo.
echo Location: %INSTALL_DIR%
echo.
echo To launch SyncGuard (no console window):
echo   Double-click: %INSTALL_DIR%\SyncGuard.pyw
echo.
echo To launch with console (for debugging):
echo   cd "%INSTALL_DIR%"
echo   python syncguard_protected.py
echo.
echo To update later:
echo   cd "%INSTALL_DIR%"
echo   git pull origin main
echo.

:: ── Desktop shortcut ──────────────────────────────────────────
set /p "SHORTCUT= Create Desktop shortcut? (Y/N): "
if /i not "!SHORTCUT!"=="Y" goto :skip_shortcut
echo [v] Creating Desktop shortcut...

:: Write PowerShell script to temp file
>"%TEMP%\syncguard_shortcut.ps1" echo $ws = New-Object -ComObject WScript.Shell
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc = $ws.CreateShortcut([System.IO.Path]::Combine([System.IO.Path]::GetFolderPath('Desktop'), 'SyncGuard.lnk'))
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.TargetPath = '%PYTHONW%'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.Arguments = '"'%INSTALL_DIR%\SyncGuard.pyw'"'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.WorkingDirectory = '%INSTALL_DIR%'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.Description = 'SyncGuard - FreeFileSync Job Manager'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.Save()

powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\syncguard_shortcut.ps1"
del "%TEMP%\syncguard_shortcut.ps1" 2>nul
echo [i] Desktop shortcut created.
:skip_shortcut

echo.
set /p "LAUNCH= Launch SyncGuard now? (Y/N): "
if /i not "!LAUNCH!"=="Y" goto :skip_launch
cd /d "%INSTALL_DIR%"
start "" %PYTHONW% "SyncGuard.pyw"
:skip_launch
endlocal
