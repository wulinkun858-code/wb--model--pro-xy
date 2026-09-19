@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   离线自测（不联网、不需要凭据）
echo.
python test_wb_proxy.py
pause
