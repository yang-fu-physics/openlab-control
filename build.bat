@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm OpenLabControl.spec
if errorlevel 1 goto :error
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "tools\stage_windows_release.ps1"
if errorlevel 1 goto :error
echo.
echo Build completed: dist\OpenLabControl\OpenLabControl.exe
echo Instrument scanner: dist\OpenLabControl\InstrumentScanner.exe
if defined CI exit /b 0
pause
exit /b 0

:error
echo.
echo Build failed.
if defined CI exit /b 1
pause
exit /b 1
