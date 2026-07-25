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
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765'"
"%PYTHON_EXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
if not errorlevel 1 exit /b 0

echo.
echo The server stopped because an error occurred.
pause
exit /b 1
