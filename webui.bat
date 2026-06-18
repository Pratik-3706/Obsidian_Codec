@echo off
title Obsidian Codec WebUI
if not exist "venv" (
    echo [ERROR] Virtual environment 'venv' not found. Please run setup.bat first!
    pause
    exit /b 1
)
echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat
echo [INFO] Starting Web UI backend server...
python obsidian_codec/src/web_ui/webui.py
pause