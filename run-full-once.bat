@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if exist "%PYTHON_EXE%" goto run

echo ERROR: .venv was not found in:
echo %CD%
echo Install the project dependencies according to README.md first.
pause
exit /b 1

:run
"%PYTHON_EXE%" -m app.full_run
if not errorlevel 1 exit /b 0

echo.
echo Errors occurred during the full run. This window will remain open.
pause
exit /b 1
