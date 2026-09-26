@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_CMD="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo Python 3.11 or newer is required: https://www.python.org/downloads/
    pause
    exit /b 1
)

%PYTHON_CMD% -m pip --version >nul 2>&1
if errorlevel 1 %PYTHON_CMD% -m ensurepip --upgrade
if errorlevel 1 (
    echo pip is unavailable.
    pause
    exit /b 1
)

%PYTHON_CMD% -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

%PYTHON_CMD% -m PyInstaller --version >nul 2>&1
if errorlevel 1 %PYTHON_CMD% -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 (
    echo PyInstaller installation failed.
    pause
    exit /b 1
)

set "DATA_ARGS="
if exist "assets\" set "DATA_ARGS=--add-data=assets;assets"

%PYTHON_CMD% -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --name TF2ChatTranslator ^
    --collect-all langdetect ^
    --collect-submodules pynput ^
    %DATA_ARGS% ^
    "main.py"

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

copy /Y "config.example.json" "dist\config.example.json" >nul
if errorlevel 1 (
    echo Build succeeded, but config.example.json could not be copied.
    pause
    exit /b 1
)

echo EXE: %CD%\dist\TF2ChatTranslator.exe
echo config.json is NOT bundled. Copy config.example.json to dist\config.json
echo and enter your own keys beside the EXE before use.
pause
exit /b 0
