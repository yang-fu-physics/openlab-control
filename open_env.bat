@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" (
    echo Activating Python environment...
    cmd /k ".venv\Scripts\activate.bat"
) else (
    echo Virtual environment not found. Please run setup.bat first.
    pause
)
