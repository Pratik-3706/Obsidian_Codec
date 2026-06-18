#!/bin/bash
if [ ! -d "venv" ]; then
    echo "[ERROR] Virtual environment 'venv' not found. Please run ./setup.sh first!"
    exit 1
fi
echo "[INFO] Activating virtual environment..."
source venv/bin/activate
echo "[INFO] Starting Web UI backend server..."
python3 obsidian_codec/src/web_ui/webui.py
