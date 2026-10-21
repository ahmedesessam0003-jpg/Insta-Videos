@echo off
rem Double-click to run with the settings in products.json,
rem or run from a command prompt with options, e.g.:  run.bat --dry-run
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment ^(first run only^)...
    %PY% -m venv .venv || goto :nopython
)
echo Installing / updating requirements...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -U -r requirements.txt || goto :failed
".venv\Scripts\python.exe" insta_videos.py %*
echo.
pause
exit /b

:nopython
echo.
echo Python 3 was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during installation, then run this file again.
pause
exit /b 1

:failed
echo.
echo Installing the requirements failed. Check your internet connection and try again.
pause
exit /b 1
