@echo off
chcp 65001 >nul
cd /d "%~dp0"
title wb-model-proxy
echo.
echo   正在启动 wb-model-proxy ...
echo.
python wb_proxy.py %*
echo.
echo   已退出。
pause
