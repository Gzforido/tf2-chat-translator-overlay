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

%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Python 3.11 or newer is required.
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

%PYTHON_CMD% -c "import PyQt6, deepl, langdetect, pynput" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    %PYTHON_CMD% -m pip install --disable-pip-version-check -r "requirements.txt"
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

%PYTHON_CMD% -m utils.setup_api --check-required >nul 2>&1
if errorlevel 1 (
    echo Local API setup is required.
    call "%~dp0setup_api.bat"
    if errorlevel 1 exit /b 1
)

%PYTHON_CMD% -m utils.setup_api --check-required >nul 2>&1
if errorlevel 1 (
    echo DeepL API key is still missing. Run setup_api.bat again.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "from utils.setup_tf2 import run; run()"
echo Starting TF2 Chat Translator Overlay...
%PYTHON_CMD% "main.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo Application exited with code %APP_EXIT_CODE%.
    pause
)
exit /b %APP_EXIT_CODE%
