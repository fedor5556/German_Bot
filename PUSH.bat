@echo off
REM One-click "save to GitHub": stages everything, commits, and pushes.
REM Secrets/data never go up -- .gitignore excludes .env, data/, logs/, venv/.
REM Double-click this any time you want to back up your work to the repo.
setlocal EnableExtensions
cd /d "%~dp0"

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto notrepo

echo ============================================
echo   Push German Bot to GitHub
echo ============================================
echo Remote:
git remote get-url origin
echo.

echo Changes to be saved:
git status --short
echo.

set "MSG="
set /p "MSG=Commit message (press Enter for auto): "
if not defined MSG set "MSG=Update %DATE% %TIME%"

echo.
echo [INFO] Staging all changes...
git add -A

git diff --cached --quiet
if errorlevel 1 goto docommit
echo [INFO] No new changes to commit -- will still push any earlier commits.
goto dopush

:docommit
git commit -m "%MSG%"
if errorlevel 1 goto commitfail

:dopush
echo.
echo [INFO] Pushing to GitHub...
git push
if errorlevel 1 goto pushfail

echo.
echo [DONE] Your work is safely on GitHub.
goto end

:notrepo
echo [FATAL] This folder is not a git repository (or git is not installed).
goto end

:commitfail
echo [FATAL] Commit failed. Nothing was pushed.
goto end

:pushfail
echo [FATAL] Push failed. Check your internet / GitHub login and try again.
goto end

:end
echo.
echo Press any key to close.
pause >nul
