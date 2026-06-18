@echo off
title Obsidian Codec CLI
if not exist "venv" (
    echo [ERROR] Virtual environment 'venv' not found. Please run setup.bat first!
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
python obsidian_codec/src/cmd_line/cli.py %*
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Execution failed with exit code %errorlevel%
    pause
)
