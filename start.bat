@echo off
REM Starts the dispatch bot. Close this window or press Ctrl+C to stop it gracefully.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo [ERROR] Run setup.bat first.
  pause & exit /b 1
)
if not exist .env (
  echo [ERROR] .env not found. Copy .env.example to .env and fill it in.
  pause & exit /b 1
)
title ER:LC AI Dispatch
".venv\Scripts\python.exe" main.py
echo.
echo The bot has stopped. See logs\dispatch.log for details.
pause
