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
REM Optional: start the ngrok tunnel for the Netlify dashboard (set NGROK_URL in .env, e.g. NGROK_URL=your-name.ngrok-free.dev).
set "NGROK_URL="
set "DASHBOARD_PORT=8080"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
  if /i "%%a"=="NGROK_URL" set "NGROK_URL=%%b"
  if /i "%%a"=="DASHBOARD_PORT" set "DASHBOARD_PORT=%%b"
)
if defined NGROK_URL (
  where ngrok >nul 2>nul
  if errorlevel 1 (
    echo [WARN] NGROK_URL is set but ngrok isn't installed. Install it with: winget install ngrok.ngrok
  ) else (
    tasklist /fi "imagename eq ngrok.exe" | find /i "ngrok.exe" >nul
    if errorlevel 1 (
      echo Starting the ngrok tunnel for the dashboard: https://%NGROK_URL%
      start "Hive Dispatch tunnel (ngrok) - keep open" ngrok http --url=%NGROK_URL% %DASHBOARD_PORT%
    ) else (
      echo ngrok is already running.
    )
  )
)
title ER:LC AI Dispatch
".venv\Scripts\python.exe" main.py
echo.
echo The bot has stopped. See logs\dispatch.log for details.
pause
