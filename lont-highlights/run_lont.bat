@echo off
rem Double-click to download the story highlights of instagram.com/lont.sa into output\
rem Data only, nothing downloaded (from a command prompt in this folder):  run_lont.bat --dry-run
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem The same Python environment as the Natural Roots scripts in the folder above.
set "VENV=..\.venv"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist "%VENV%\Scripts\python.exe" (
    echo Creating the Python environment ^(first run only^)...
    %PY% -m venv "%VENV%" || goto :nopython
)
echo Installing / updating requirements...
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -U -r requirements.txt || goto :failed
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -U pillow pillow-heif || echo WARNING: pillow-heif could not be installed, so HEIC pictures will stay .heic
"%VENV%\Scripts\python.exe" lont_highlights.py %*
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
