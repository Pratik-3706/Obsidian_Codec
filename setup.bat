@echo off
echo ===================================================
echo   Obsidian_Codec - Virtual Environment Setup
echo ===================================================
echo.

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in your PATH.
    echo Please install Python 3.7+ and try again.
    pause
    exit /b 1
)

:: Check for FFmpeg
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] FFmpeg was not detected in your PATH.
    echo Obsidian Codec requires FFmpeg and FFprobe to process media.
    echo Please make sure FFmpeg is installed and added to your system environment variables.
    echo.
)

:: Create venv if not exists
if not exist "venv" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [SUCCESS] Virtual environment created.
) else (
    echo [INFO] Virtual environment already exists.
)

:: Activate and install requirements
echo [INFO] Activating virtual environment...
call venv\Scripts\activate.bat

echo [INFO] Upgrading pip...
python -m pip install --upgrade pip

echo [INFO] Installing requirements from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo   [SUCCESS] Setup Completed Successfully!
echo   You can now run:
echo   - webui.bat  (to launch the Web UI)
echo   - CLI.bat    (to use the CLI interface)
echo ===================================================
echo.
pause
