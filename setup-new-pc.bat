@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo   ClipForge - Setup on a new PC / Laptop
echo ============================================================
echo.
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.12 from https://www.python.org/downloads/
    echo (Make sure to check "Add Python to PATH" during installation)
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment (.venv)...
python -m venv .venv

echo [2/3] Installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .

echo [3/3] Testing ClipForge...
python -m clipforge doctor

echo.
echo ============================================================
echo   Setup Complete! You can now run start-ui.bat
echo ============================================================
pause

