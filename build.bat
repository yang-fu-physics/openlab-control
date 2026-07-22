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
xcopy /E /I /Y "configs" "dist\OpenLabControl\configs" >nul
xcopy /E /I /Y "examples" "dist\OpenLabControl\examples" >nul
xcopy /E /I /Y "docs" "dist\OpenLabControl\docs" >nul
xcopy /E /I /Y "plugin_templates" "dist\OpenLabControl\plugin_templates" >nul
copy /Y "README.md" "dist\OpenLabControl\README.md" >nul
copy /Y "CHANGELOG.md" "dist\OpenLabControl\CHANGELOG.md" >nul
copy /Y "SECURITY.md" "dist\OpenLabControl\SECURITY.md" >nul
if not exist "dist\OpenLabControl\runs" mkdir "dist\OpenLabControl\runs"
echo.
echo Build completed: dist\OpenLabControl\OpenLabControl.exe
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
