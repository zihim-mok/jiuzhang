@echo off
chcp 65001 >nul
title 九账算术 - 演示服务 (http://127.0.0.1:8000/ui)
cd /d "%~dp0"

echo ============================================================
echo   九账算术 · 演示服务
echo   模拟银行环境 / 无真实资金通道
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 找不到虚拟环境，请先运行 deploy.ps1
  pause
  exit /b 1
)

if not exist "app\data\banking_sim.db" (
  echo [提示] 演示数据库不存在，正在重建...
  ".venv\Scripts\python.exe" -m app.db.init_db --reset
)

echo 启动后浏览器打开： http://127.0.0.1:8000/ui
echo 停止服务：在本窗口按 Ctrl + C
echo.
echo 需要重置演示数据时，另开一个窗口执行：
echo    .venv\Scripts\python.exe -m app.db.init_db --reset
echo ------------------------------------------------------------
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo 服务已停止。
pause
