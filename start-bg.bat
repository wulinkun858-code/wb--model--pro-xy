@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 在一个最小化的独立窗口里常驻运行代理。
rem 为什么要它：代理一关，Claude Code / Codex 就会报 Connection refused。
rem 这个脚本会开一个【最小化】的窗口挂着代理，不占你当前终端，也不容易误关。

start "wb-model-proxy" /min cmd /k python wb_proxy.py %*

echo.
echo   已在最小化的新窗口启动 wb-model-proxy（窗口标题：wb-model-proxy）。
echo   验证是否就绪：curl http://127.0.0.1:8788/health
echo   停止方式：运行 stop-bg.bat，或直接关掉那个最小化窗口。
echo.
timeout /t 2 >nul
