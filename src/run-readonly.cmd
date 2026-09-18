@echo off
REM 证闻 · 免登录只读演示入口（面向复赛线上评选的评委访问）
REM 只读约束由服务端强制：拒绝一切写操作；只读模式下不写会话状态（零副作用）
setlocal
cd /d "%~dp0"

if "%HOST%"=="" set HOST=127.0.0.1
if "%PORT%"=="" set PORT=8848
set READONLY=1

echo [证闻] 只读演示模式启动中...  演示入口 http://%HOST%:%PORT%/demo
python app.py --readonly
if errorlevel 1 (
  echo.
  echo [证闻] 启动失败。请确认已安装 Python 3.10+ 且 python 在 PATH 中。
  pause
)
endlocal
