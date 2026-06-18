#!/bin/bash
if [ ! -d "venv" ]; then
    echo "[ERROR] Virtual environment 'venv' not found. Please run ./setup.sh first!"
    exit 1
fi
source venv/bin/activate
python3 obsidian_codec/src/cmd_line/cli.py "$@"
