@echo off
REM One-time setup (Windows): creates a Python 3.9 virtualenv in .venv and installs deps.
REM Usage:  setup.bat        then run the bot with:  .venv\Scripts\python bots.py
cd /d "%~dp0"

py -3.9 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python 3.9 not found. Install it from https://www.python.org/downloads/release/python-3913/
  echo        ^(tick "Add python.exe to PATH" / py launcher^), then re-run setup.bat
  exit /b 1
)

py -3.9 -m venv .venv || exit /b 1
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\python -m pip install --quiet -r requirements.txt || exit /b 1
if not exist config.json copy config.example.json config.json >nul

echo.
echo Done. Edit config.json with your bot tokens, then run:
echo   .venv\Scripts\python bots.py
