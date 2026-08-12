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
if not exist "dist\OpenLabControl\tools" mkdir "dist\OpenLabControl\tools"
copy /Y "dist\InstrumentScanner.exe" "dist\OpenLabControl\tools\InstrumentScanner.exe" >nul
if errorlevel 1 goto :error
xcopy /E /I /Y "configs" "dist\OpenLabControl\configs" >nul
xcopy /E /I /Y "examples" "dist\OpenLabControl\examples" >nul
xcopy /E /I /Y "docs" "dist\OpenLabControl\docs" >nul
xcopy /E /I /Y "templates" "dist\OpenLabControl\templates" >nul
xcopy /E /I /Y "integrations" "dist\OpenLabControl\integrations" >nul
xcopy /E /I /Y "modules" "dist\OpenLabControl\modules" >nul
for %%R in (configs examples docs templates integrations modules) do call :remove_python_caches "dist\OpenLabControl\%%R"
copy /Y "README.md" "dist\OpenLabControl\README.md" >nul
copy /Y "CHANGELOG.md" "dist\OpenLabControl\CHANGELOG.md" >nul
copy /Y "SECURITY.md" "dist\OpenLabControl\SECURITY.md" >nul
if not exist "dist\OpenLabControl\runs" mkdir "dist\OpenLabControl\runs"
if not exist "dist\OpenLabControl\module_data" mkdir "dist\OpenLabControl\module_data"
if not exist "dist\OpenLabControl\wheels" mkdir "dist\OpenLabControl\wheels"
if not exist "dist\OpenLabControl\modules" mkdir "dist\OpenLabControl\modules"
if not exist "dist\OpenLabControl\system_instruments" mkdir "dist\OpenLabControl\system_instruments"
if not exist "dist\OpenLabControl\runtime_packages" mkdir "dist\OpenLabControl\runtime_packages"
if not exist "dist\OpenLabControl\trust_state" mkdir "dist\OpenLabControl\trust_state"
echo.
echo Build completed: dist\OpenLabControl\OpenLabControl.exe
echo Instrument scanner: dist\OpenLabControl\tools\InstrumentScanner.exe
pause
exit /b 0

:remove_python_caches
for /d /r "%~1" %%D in (__pycache__) do @if exist "%%D" rd /s /q "%%D"
for /d /r "%~1" %%D in (.pytest_cache) do @if exist "%%D" rd /s /q "%%D"
for /d /r "%~1" %%D in (.mypy_cache) do @if exist "%%D" rd /s /q "%%D"
for /d /r "%~1" %%D in (.ruff_cache) do @if exist "%%D" rd /s /q "%%D"
del /s /q "%~1\*.pyc" >nul 2>&1
del /s /q "%~1\*.pyo" >nul 2>&1
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
