@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto :missing
".venv\Scripts\python.exe" run.py
set "OPENLAB_EXIT=%ERRORLEVEL%"
pause
exit /b %OPENLAB_EXIT%

:missing
echo Python environment not found. Run setup.bat first.
pause
exit /b 1
