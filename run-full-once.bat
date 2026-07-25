@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到 .venv，请先按照 README 安装项目。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.full_run
if errorlevel 1 (
  echo.
  echo 运行中存在错误，窗口将保持打开。按任意键后才会关闭。
  pause
)
