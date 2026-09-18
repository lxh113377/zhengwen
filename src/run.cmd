@echo off
REM 证闻 · 一键启动（Windows）
REM 零第三方依赖 —— 任何装有 Python 3.10+ 的机器可直接运行
setlocal
cd /d "%~dp0"

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8848

echo [证闻] 启动中...  http://%HOST%:%PORT%/
python app.py
if errorlevel 1 (
  echo.
  echo [证闻] 启动失败。请确认已安装 Python 3.10+ 且 python 在 PATH 中。
  pause
)
endlocal
