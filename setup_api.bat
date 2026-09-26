@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Official pages for your own API credentials:
echo DeepL: https://www.deepl.com/en/developers
echo Steam Web API: https://steamcommunity.com/dev/apikey
echo backpack.tf API and token: https://next.backpack.tf/account/api-access
echo.
echo Credentials are saved only in local config.json, not in Python code.
echo.

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

%PYTHON_CMD% -m utils.setup_api
set "SETUP_EXIT_CODE=%ERRORLEVEL%"
if not "%SETUP_EXIT_CODE%"=="0" echo Setup failed with code %SETUP_EXIT_CODE%.
pause
exit /b %SETUP_EXIT_CODE%
