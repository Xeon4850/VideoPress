@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r "%~dp0requirements.txt" pyinstaller
if errorlevel 1 goto error
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "VideoPress" --collect-all tkinterdnd2 "%~dp0videopress.py"
if errorlevel 1 goto error
echo.
echo Build completed: dist\VideoPress.exe
echo Copy ffmpeg-latest and ffmpeg-compat next to the EXE.
pause
exit /b 0
:error
echo Build failed.
pause
exit /b 1
