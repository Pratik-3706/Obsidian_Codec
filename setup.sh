#!/bin/bash
set -e

echo "==================================================="
echo "  Obsidian_Codec - Virtual Environment Setup"
echo "==================================================="
echo ""

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python 3 is not installed or not in your PATH."
    echo "Please install Python 3.7+ and try again."
    exit 1
fi

# Check for FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "[WARNING] FFmpeg was not detected in your PATH."
    echo "Obsidian Codec requires FFmpeg and FFprobe to process media."
    echo "Please make sure FFmpeg is installed and added to your PATH."
    echo ""
fi

# Create venv if not exists
if [ ! -d "venv" ]; then
    echo "[INFO] Creating virtual environment..."
    python3 -m venv venv
    echo "[SUCCESS] Virtual environment created."
else
    echo "[INFO] Virtual environment already exists."
fi

# Activate and install requirements
echo "[INFO] Activating virtual environment..."
source venv/bin/activate

echo "[INFO] Upgrading pip..."
python3 -m pip install --upgrade pip

echo "[INFO] Installing requirements from requirements.txt..."
pip install -r requirements.txt

echo ""
echo "==================================================="
echo "  [SUCCESS] Setup Completed Successfully!"
echo "  You can now run:"
echo "  - ./webui.sh  (to launch the Web UI)"
echo "  - ./CLI.sh    (to use the CLI interface)"
echo "==================================================="
echo ""
