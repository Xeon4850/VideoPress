@echo off
setlocal
cd /d "%~dp0"
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
%PY% -c "import tkinterdnd2" >nul 2>nul
if errorlevel 1 %PY% -m pip install --index-url https://pypi.org/simple "tkinterdnd2>=0.4.2"
%PY% "%~dp0videopress.py"
if errorlevel 1 (
    echo.
    echo VideoPress exited with an error. Run setup.bat and check setup.log.
    pause
)
exit /b 0
:nopython
echo Python was not found. Run setup.bat for details.
pause
exit /b 1
