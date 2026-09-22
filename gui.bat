@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo 没找到 .venv\Scripts\pythonw.exe
    echo 请先按 README 里的说明创建虚拟环境并安装 requirements.txt
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" gui.py
