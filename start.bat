@echo off
REM ─────────────────────────────────────────────────────────────────
REM  Airbnb Room Analyzer – Windows launcher
REM ─────────────────────────────────────────────────────────────────
cd /d "%~dp0"

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo ERROR: Python is not installed.
  echo Download it from https://www.python.org/downloads/
  echo Make sure to check "Add Python to PATH" during install.
  pause
  exit /b 1
)

if not exist venv (
  echo Creating virtual environment...
  python -m venv venv
)

call venv\Scripts\activate.bat

python -c "import flask, google.genai, PIL, playwright" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo Installing dependencies (one-time setup)...
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  echo Installing Chromium browser for Playwright...
  playwright install chromium
)

echo.
echo   ==========================================
echo    Airbnb Room Analyzer
echo    Opening http://localhost:5000
echo    Close this window to quit
echo   ==========================================
echo.

python app.py
pause
