@echo off
REM One-shot venv recovery (guide s6.2). Body of the Hub's /recreate_venv.
cd /d "%~dp0"
echo [INFO] Removing old venv...
if exist "venv" rmdir /s /q "venv"
python -m venv venv || py -m venv venv
if not exist "venv\Scripts\python.exe" (
    echo [FATAL] venv creation failed. Is Python 3.11+ on PATH?
    pause
    exit /b 1
)
set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
"%PYTHON_CMD%" -m ensurepip --upgrade
"%PYTHON_CMD%" -m pip install --upgrade pip
"%PYTHON_CMD%" -m pip install -r requirements.txt || (
    echo [FATAL] pip install failed.
    pause
    exit /b 1
)
echo [INFO] venv rebuilt. Launching...
call "%~dp0COMPLETE_LAUNCH.bat"
