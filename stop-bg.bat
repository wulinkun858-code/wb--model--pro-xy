@echo off
chcp 65001 >nul
rem 停掉占用 8788 端口的代理进程。
rem 端口改过的话，把下面的 8788 一起改成新端口。

set PORT=8788
set FOUND=

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    echo   结束进程 PID=%%a
    taskkill /PID %%a /F >nul 2>&1
    set FOUND=1
)

if not defined FOUND (
    echo   没有发现监听 %PORT% 端口的进程 —— 代理本来就没在跑。
) else (
    echo   已停止 wb-model-proxy。
)
echo.
pause
