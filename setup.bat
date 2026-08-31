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
if not errorlevel 1 goto :git_ok

echo [!] Git is not installed.
echo.
set /p "INSTALL_GIT= Install Git automatically? [Y/N]: "
if /i not "!INSTALL_GIT!"=="Y" goto :no_git
echo.
echo [v] Downloading Git for Windows...
set "GIT_INSTALLER=%TEMP%\git_installer.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.1/Git-2.55.0-64-bit.exe' -OutFile '%GIT_INSTALLER%' -UseBasicParsing"
if not exist "%GIT_INSTALLER%" (
    echo [X] Download failed. Install Git manually from: https://git-scm.com/download/win
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
    echo [!] Git installed but not yet in PATH. Please restart this script.
    exit /b 1
)

:git_ok
echo [i] Git found: OK

:: ── Check for Python ───────────────────────────────────────────
where python >nul 2>&1
if not errorlevel 1 goto :python_ok
where python3 >nul 2>&1
if not errorlevel 1 (
    set "PYTHON=python3"
    goto :python_ok
)

echo.
echo [!] Python is not installed.
echo.
set /p "INSTALL_PY= Install Python automatically? [Y/N]: "
if /i not "!INSTALL_PY!"=="Y" goto :no_py
echo.
echo [v] Downloading Python...
set "PY_INSTALLER=%TEMP%\python_installer.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe' -OutFile '%PY_INSTALLER%' -UseBasicParsing"
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
set "PATH=%PATH%;C:\Python313;C:\Python313\Scripts;C:\Program Files\Python313;C:\Program Files (x86)\Python313"
where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python installed but not yet in PATH. Please restart this script.
    exit /b 1
)
set "PYTHON=python"
goto :python_ok

:no_py
echo [X] Python is required. Download from: https://www.python.org/downloads/
exit /b 1

:no_git
echo [X] Git is required. Download from: https://git-scm.com/download/win
exit /b 1

:python_ok
if not defined PYTHON set "PYTHON=python"
echo [i] Python found: OK

:: Show Python version
for /f "tokens=*" %%v in ('%PYTHON% --version 2^>^&1') do set "PYVER=%%v"
echo [i] %PYVER%

:: Locate pythonw.exe
set "PYTHONW="
for /f "tokens=*" %%w in ('where pythonw 2^>nul') do (
    if not defined PYTHONW set "PYTHONW=%%w"
)
if not defined PYTHONW set "PYTHONW=%PYTHON%"
echo.

:: ── Check if folder already has a git repo ─────────────────────
if not exist "%INSTALL_DIR%\.git" goto :not_a_repo
echo [!] %INSTALL_DIR% already contains a SyncGuard git repository.
echo.
set /p "CHOICE= Run update instead? [Y/N]: "
if /i not "!CHOICE!"=="Y" (
    echo [i] Skipping clone.
    goto :install_deps
)
cd /d "%INSTALL_DIR%"
echo.
echo [v] Pulling latest changes...
git pull origin main
if errorlevel 1 (
    echo [X] Update failed. Check for local conflicts.
    exit /b 1
)
goto :install_deps

:not_a_repo
:: ── Folder exists but is not a git repo ────────────────────────
if not exist "%INSTALL_DIR%" goto :fresh_clone
echo [!] %INSTALL_DIR% exists but is not a git repository.
echo.
set /p "CHOICE= Initialize git and pull SyncGuard? [Y/N]: "
if /i not "!CHOICE!"=="Y" (
    echo [i] Skipping. Installing dependencies only.
    goto :install_deps
)
cd /d "%INSTALL_DIR%"
git init
git remote add origin https://github.com/HempsSA/SyncGuard.git
git fetch origin
git checkout -b main origin/main
goto :install_deps

:fresh_clone
:: ── Fresh install — clone into a temp folder then move ─────────
echo [v] Cloning SyncGuard repository...
set "TEMP_CLONE=%INSTALL_DIR%_clone_%RANDOM%"
git clone https://github.com/HempsSA/SyncGuard.git "%TEMP_CLONE%"
if errorlevel 1 (
    echo [X] Clone failed. Check your internet connection.
    rmdir /s /q "%TEMP_CLONE%" 2>nul
    exit /b 1
)
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
    echo Try: %PYTHON% -m pip install -r "%INSTALL_DIR%\requirements.txt"
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
set /p "SHORTCUT= Create Desktop shortcut? [Y/N]: "
if /i not "!SHORTCUT!"=="Y" goto :skip_shortcut
echo [v] Creating Desktop shortcut...

>"%TEMP%\syncguard_shortcut.ps1" echo $ws = New-Object -ComObject WScript.Shell
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc = $ws.CreateShortcut([System.IO.Path]::Combine([System.Environment]::GetFolderPath('Desktop'), 'SyncGuard.lnk'))
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.TargetPath = '%PYTHONW%'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.Arguments = '%INSTALL_DIR%\SyncGuard.pyw'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.WorkingDirectory = '%INSTALL_DIR%'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.IconLocation = '%INSTALL_DIR%\assets\sync_icon.ico,0'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.Description = 'SyncGuard - FreeFileSync Job Manager'
>>"%TEMP%\syncguard_shortcut.ps1" echo $sc.Save()

powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\syncguard_shortcut.ps1"
del "%TEMP%\syncguard_shortcut.ps1" 2>nul
echo [i] Desktop shortcut created.
:skip_shortcut

echo.
set /p "LAUNCH= Launch SyncGuard now? [Y/N]: "
if /i not "!LAUNCH!"=="Y" goto :skip_launch
cd /d "%INSTALL_DIR%"
start "" %PYTHONW% "SyncGuard.pyw"
:skip_launch
endlocal
