@echo off
rem Turns the downloaded videos in output\ into pictures (one frame each), except the ones after --keep.
rem From a command prompt in this folder:  run_videos_to_photos.bat --keep lont-hl-03-001
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem The same Python environment as run_lont.bat.
set "VENV=..\.venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo Run run_lont.bat first: it sets up Python and downloads the highlights.
    pause
    exit /b 1
)
echo Installing / updating the video tool...
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -U imageio-ffmpeg || goto :failed
"%VENV%\Scripts\python.exe" videos_to_photos.py %*
echo.
pause
exit /b

:failed
echo.
echo Installing the video tool failed. Check your internet connection and try again.
pause
exit /b 1
