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
set "RUN_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%RUN_EXIT_CODE%"=="0" (
  echo Full run finished successfully. The program has exited.
) else (
  echo Full run finished with errors. Review the output above.
)
echo Press any key to close this window.
pause
exit /b %RUN_EXIT_CODE%
