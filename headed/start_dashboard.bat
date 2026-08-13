@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [dashboard] 启动管理面板 http://127.0.0.1:8765
echo [dashboard] 可在面板里开关住宅IP、看每个邮箱注册进度、启动任务
echo.
python dashboard.py
pause
