@echo off
REM Surgical stop -- folder-path AND german_bot.py only (guide s7). No restart.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_processes.ps1"
