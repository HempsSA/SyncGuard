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

:: ── Check for curl (needed for fast downloads) ──────────────────
where curl >nul 2>&1
if errorlevel 1 (
    echo [!] curl not found. Using PowerShell for downloads ^(slower^).
    set "USE_CURL=0"
) else (
    set "USE_CURL=1"
)

:: ── Check for Git ──────────────────────────────────────────────
where git >nul 2>&1
if not errorlevel 1 goto :git_ok

echo [!] Git is not installed.
echo.
set /p "INSTALL_GIT= Install Git automatically? [Y/N]: "
if /i not "!INSTALL_GIT!"=="Y" goto :no_git
echo.
echo [v] Downloading Git for Windows...
set "GIT_INSTALLER=%TEMP%\git_installer.exe"

if "!USE_CURL!"=="1" (
    curl -L --progress-bar -o "%GIT_INSTALLER%" "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.1/Git-2.55.0-64-bit.exe"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.1/Git-2.55.0-64-bit.exe' -OutFile '%GIT_INSTALLER%' -UseBasicParsing"
)

if not exist "%GIT_INSTALLER%" (
    echo [X] Download failed. Install Git manually from: https://git-scm.com/download/win
    exit /b 1
)

:: ── Verify file size (should be > 50MB) ────────────────────────
for %%A in ("%GIT_INSTALLER%") do set "GIT_SIZE=%%~zA"
if !GIT_SIZE! LSS 50000000 (
    echo [X] Download appears incomplete ^(!GIT_SIZE! bytes^). Please try again or install Git manually.
    del "%GIT_INSTALLER%" 2>nul
    exit /b 1
)

echo [v] Installing Git (silent)...
"%GIT_INSTALLER%" /VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS
if errorlevel 1 (
    echo [X] Git installation failed. Install Git manually.
    del "%GIT_INSTALLER%" 2>nul
    exit /b 1
)
del "%GIT_INSTALLER%" 2>nul
echo [i] Git installed successfully.
echo.
echo [!] You may need to restart this script for Git to be in PATH.
echo     If "git" is not recognized, close this window and run setup.bat again.
echo.
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files (x86)\Git\cmd"
where git >nul 2>&1
if errorlevel 1 (
    echo [!] Git installed but not in PATH yet. Continuing — will use git later if available.
)
:git_ok
echo [i] Git found: OK
echo.

:: ── Clone or update repo ───────────────────────────────────────
if exist "%INSTALL_DIR%\.git" (
    echo [i] Repo exists — pulling latest changes...
    cd /d "%INSTALL_DIR%"
    git pull --rebase origin main
    if errorlevel 1 (
        echo [!] Pull failed. Continuing with existing code.
    )
) else (
    echo [v] Cloning SyncGuard...
    git clone https://github.com/HempsSA/SyncGuard.git "%INSTALL_DIR%"
    if errorlevel 1 (
        echo [X] Clone failed. Check your internet connection.
        exit /b 1
    )
    cd /d "%INSTALL_DIR%"
)
echo.

:: ── Check for Python ───────────────────────────────────────────
where python >nul 2>&1
if not errorlevel 1 goto :python_ok

:: Try py launcher
where py >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=py"
    goto :python_ok
)

echo [!] Python is not installed.
echo.
set /p "INSTALL_PY= Install Python automatically? [Y/N]: "
if /i not "!INSTALL_PY!"=="Y" goto :no_python
echo.
echo [v] Downloading Python...
set "PY_INSTALLER=%TEMP%\python_installer.exe"

if "!USE_CURL!"=="1" (
    curl -L --progress-bar -o "%PY_INSTALLER%" "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe' -OutFile '%PY_INSTALLER%' -UseBasicParsing"
)

if not exist "%PY_INSTALLER%" (
    echo [X] Download failed. Install Python manually from: https://www.python.org/downloads/
    exit /b 1
)

echo [v] Installing Python (silent)...
"%PY_INSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_test=0
if errorlevel 1 (
    echo [X] Python installation failed. Install Python manually.
    del "%PY_INSTALLER%" 2>nul
    exit /b 1
)
del "%PY_INSTALLER%" 2>nul
echo [i] Python installed successfully.
echo.
echo [!] You may need to restart this script for Python to be in PATH.
echo     If "python" is not recognized, close this window and run setup.bat again.
echo.
set "PATH=%PATH%;C:\Program Files\Python313;C:\Program Files\Python313\Scripts;C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313;C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\Scripts"
set "PYTHON=python"
:python_ok
if not defined PYTHON set "PYTHON=python"
echo [i] Python found: OK
echo.

:: ── Install dependencies ───────────────────────────────────────
echo [v] Installing Python dependencies...
if exist "requirements.txt" (
    !PYTHON! -m pip install --upgrade pip 2>nul
    !PYTHON! -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [!] Some dependencies failed. Trying one-by-one...
        for /f "tokens=*" %%i in (requirements.txt) do (
            set "line=%%i"
            if not "!line:~0,1!"=="#" if not "!line!"=="" (
                !PYTHON! -m pip install "%%i" 2>nul
            )
        )
    )
) else (
    echo [i] No requirements.txt — skipping dependencies.
)
echo.

:: ── Desktop shortcut ───────────────────────────────────────────
where powershell >nul 2>&1
if errorlevel 1 goto :skip_shortcut
set /p "SHORTCUT= Create Desktop shortcut? [Y/N]: "
if /i not "!SHORTCUT!"=="Y" goto :skip_shortcut
echo [v] Creating Desktop shortcut...

:: Find pythonw.exe
set "PYTHONW=pythonw.exe"
where pythonw >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('where pythonw') do set "PYTHONW=%%i"
    goto :shortcut_found
)
:: Check common locations
if exist "C:\Program Files\Python313\pythonw.exe" set "PYTHONW=C:\Program Files\Python313\pythonw.exe"
if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\pythonw.exe" set "PYTHONW=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\pythonw.exe"
:shortcut_found

:: Write PowerShell script to temp file (avoids batch escaping issues)
set "PS1_FILE=%TEMP%\syncguard_shortcut.ps1"
echo $ws = New-Object -ComObject WScript.Shell > "%PS1_FILE%"
echo $desktop = [System.Environment]::GetFolderPath('Desktop') >> "%PS1_FILE%"
echo $sc = $ws.CreateShortcut([System.IO.Path]::Combine($desktop, 'SyncGuard.lnk')) >> "%PS1_FILE%"
echo $sc.TargetPath = '%PYTHONW%' >> "%PS1_FILE%"
echo $sc.Arguments = '%INSTALL_DIR%\SyncGuard.pyw' >> "%PS1_FILE%"
echo $sc.WorkingDirectory = '%INSTALL_DIR%' >> "%PS1_FILE%"
echo $sc.Description = 'SyncGuard - FreeFileSync Job Manager' >> "%PS1_FILE%"
echo $sc.IconLocation = '%INSTALL_DIR%\assets\sync_icon.ico,0' >> "%PS1_FILE%"
echo $sc.Save() >> "%PS1_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_FILE%"
del "%PS1_FILE%" 2>nul
echo [i] Desktop shortcut created.

:skip_shortcut

:: ── Done ───────────────────────────────────────────────────────
echo.
echo ========================================
echo Setup complete!
echo ========================================
echo.
echo Launch SyncGuard:
echo   Double-click SyncGuard.pyw (no console window)
echo   -- or --
echo   cd "%INSTALL_DIR%" ^&^& python syncguard_protected.py
echo.

set /p "LAUNCH= Launch SyncGuard now? [Y/N]: "
if /i "!LAUNCH!"=="Y" (
    cd /d "%INSTALL_DIR%"
    start "" "%PYTHONW%" "SyncGuard.pyw"
)
endlocal
