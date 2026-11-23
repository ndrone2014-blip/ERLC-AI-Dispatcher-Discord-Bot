@echo off
REM One-time installer: creates the virtual environment, installs dependencies,
REM downloads the default Piper voice and creates .env from the template.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python launcher "py" not found. Install Python 3.12 from https://www.python.org/downloads/ ^(tick "Add to PATH"^).
  pause & exit /b 1
)

echo === Creating virtual environment (.venv) with Python 3.12 ===
py -3.12 -m venv .venv || (echo [ERROR] Python 3.12 is required. & pause & exit /b 1)

echo === Installing dependencies (this can take a few minutes) ===
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo [ERROR] pip install failed. & pause & exit /b 1)

echo === Downloading default dispatcher voice (Piper en_US-ryan-medium, ~60 MB) ===
if not exist data\voices mkdir data\voices
".venv\Scripts\python.exe" -m piper.download_voices en_US-ryan-medium --data-dir data\voices
if errorlevel 1 echo [WARN] Voice download failed. The bot will fall back to the Windows built-in voice.

if not exist .env (
  copy .env.example .env >nul
  echo === Created .env - open it and paste your DISCORD_TOKEN and GEMINI_API_KEY ===
)

where ffmpeg >nul 2>nul || echo [WARN] FFmpeg not found. Install with:  winget install Gyan.FFmpeg   (then set FFMPEG_PATH in .env if needed)

echo.
echo Setup complete. Edit .env, then run start.bat
pause
