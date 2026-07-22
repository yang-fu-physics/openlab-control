@echo off
setlocal
cd /d "%~dp0"
if exist "dist\OpenLabControl\OpenLabControl.exe" (
    start "" "dist\OpenLabControl\OpenLabControl.exe"
    exit /b 0
)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" run.py
    exit /b 0
)
python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo PySide6 is not installed. Run setup.bat first.
    pause
    exit /b 1
)
start "" pythonw run.py
