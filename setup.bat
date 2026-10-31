@echo off
setlocal
cd /d "%~dp0"
set "LOG=%~dp0setup.log"
echo Setup started %date% %time% >"%LOG%"
echo ==========================================
echo VideoPress setup - dual FFmpeg builds
echo This window will stay open on any error.
echo ==========================================
echo.
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if "%PY%"=="python" (
    where python >nul 2>nul
    if errorlevel 1 goto nopython
)
echo [1/2] Installing bundled drag-and-drop component offline...
%PY% -m pip install --no-index "%~dp0vendor\tkinterdnd2-0.4.3-py3-none-any.whl" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo Local package failed. Trying official PyPI...
    %PY% -m pip install --index-url https://pypi.org/simple "tkinterdnd2>=0.4.2" >>"%LOG%" 2>&1
)
if errorlevel 1 (
    echo WARNING: Drag-and-drop component could not be installed.
    echo Setup will continue. Add buttons will still work in VideoPress.
)
echo [2/2] Downloading latest and GTX 1080 compatible FFmpeg...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_ffmpeg.ps1" >>"%LOG%" 2>&1
if errorlevel 1 goto error
echo.
echo Setup completed. Run run.bat to start VideoPress.
echo FFmpeg folders: ffmpeg-latest and ffmpeg-compat
echo.
pause
exit /b 0
:nopython
echo Python was not found. Install Python 3.10+ and enable Add Python to PATH.
goto error
:error
echo.
echo Setup failed. Please send setup.log for diagnosis.
echo.
pause
exit /b 1
