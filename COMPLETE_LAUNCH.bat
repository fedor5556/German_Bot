@echo off
REM German bot launcher -- Standard Project Interface (guide s5, s6, s8).
REM Self-healing venv, never falls back to system Python, fails loud, tees logs.
cd /d "%~dp0"

REM --- self-healing venv (create if missing; NEVER use system Python) ---
if not exist "venv\Scripts\python.exe" (
    echo [INFO] venv missing - creating it...
    python -m venv venv || py -m venv venv
)
if not exist "venv\Scripts\python.exe" (
    echo [FATAL] Could not create a virtual environment.
    echo [FATAL] Is Python 3.11+ installed and on PATH?
    pause
    exit /b 1
)
set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"

REM --- dependencies: upgrade pip first, no --quiet, fail loud ---
"%PYTHON_CMD%" -m pip install --upgrade pip
"%PYTHON_CMD%" -m pip install -r requirements.txt || (
    echo [FATAL] pip install failed -- not launching on a broken environment.
    pause
    exit /b 1
)

REM --- log housekeeping: drop .log files older than 14 days ---
if not exist "logs" mkdir "logs"
forfiles /p "logs" /m *.log /d -14 /c "cmd /c del @path" >nul 2>&1

REM --- launch unbuffered; the bot OWNS logs\german_bot.log (rotating handler).
REM Do NOT tee here: the runner spawns this with stdout=DEVNULL, and a second
REM writer would corrupt the rotating file. The bot logs the same either way.
echo [INFO] Launching German bot...
"%PYTHON_CMD%" -u german_bot.py
