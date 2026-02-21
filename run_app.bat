@echo off
title VFS Global Auto-Booking
echo ============================================
echo   VFS Global Guinea-Bissau Auto-Booking
echo ============================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo ERROR: Could not create venv. & pause & exit /b 1 )
    echo Done.
)

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Install / update dependencies
echo Installing dependencies...
pip install setuptools --quiet
pip install undetected-chromedriver selenium pandas requests --quiet
if errorlevel 1 ( echo ERROR: pip install failed. & pause & exit /b 1 )

echo.
echo Starting booking script...
echo.

REM Run the unified booking script (multi-client, nodriver-based)
REM Useful flags:
REM   --warmup              warm Cloudflare session before booking window opens
REM   --headless            hide browser (not recommended, CF may block)
REM   --sequential          book one client at a time
REM   --max-clients N       limit number of clients
REM   --csv path/to/file    use a different clients CSV
python unified_booking.py %*

echo.
echo Booking script finished.
pause
