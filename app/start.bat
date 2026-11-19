@echo off
REM Stream Clipper: one-command start. Docker if it is running, else native Python.
REM   start.bat          native Python: backend + Clipwatch console at http://localhost:8000
REM   start.bat docker   Docker instead
setlocal
cd /d "%~dp0"
if /i "%1"=="backend" goto backend
if /i "%1"=="ui" goto ui
if /i "%1"=="lineup" goto lineup

if not exist .env (
  copy /y .env.example .env >nul
  echo Created .env from .env.example . Put your free Gemini key in it: GEMINI_API_KEY=...
)
findstr /b "CLAUDE_HOME=" .env >nul || (echo CLAUDE_HOME=%USERPROFILE:\=/%/.claude>>.env)

if /i "%1"=="docker" (
  docker info >nul 2>&1 && (
    echo [docker] building and starting backend + dashboard...
    docker compose up --build -d backend frontend || exit /b 1
    echo Web UI: http://localhost:5175
    exit /b 0
  )
  echo [docker] not running - falling back to native Python.
)

REM ---- native ---------------------------------------------------------------
where ffmpeg >nul 2>&1 || (echo ffmpeg not on PATH. Install: winget install Gyan.FFmpeg & exit /b 1)

if not exist .venv\Scripts\python.exe (
  echo [native] creating Python 3.11 venv ^(first run installs ~2 GB of CPU wheels^)...
  uv venv --python 3.11 .venv || py -3.11 -m venv .venv || exit /b 1
  .venv\Scripts\python -m ensurepip -q 2>nul
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 torchvision==0.26.0 || exit /b 1
  .venv\Scripts\python -m pip install -q --no-deps -r requirements-cpu.lock || exit /b 1
  .venv\Scripts\python -m pip install -q -r requirements.txt -r requirements-billing.txt "yt-dlp[default]" || exit /b 1
)

if not exist tools\TwitchDownloaderCLI.exe (
  echo [native] fetching TwitchDownloaderCLI 1.56.5...
  powershell -NoProfile -Command "New-Item -ItemType Directory -Force tools | Out-Null; Invoke-WebRequest https://github.com/lay295/TwitchDownloader/releases/download/1.56.5/TwitchDownloaderCLI-1.56.5-Windows-x64.zip -OutFile tools\td.zip; Expand-Archive -Force tools\td.zip tools; Remove-Item tools\td.zip"
)


echo [native] backend starting. Clipwatch console: http://localhost:8000/console
start "clipwatch backend" cmd /k ""%~f0" backend"
timeout /t 15 /nobreak >nul
start "" http://localhost:8000/console
exit /b 0

:backend
REM win_compat holds a Windows stand-in for the Unix-only fcntl module.
set "PYTHONPATH=%~dp0win_compat"
set "TWITCH_DOWNLOADER_CLI=%~dp0tools\TwitchDownloaderCLI.exe"
set PYTHONIOENCODING=utf-8
REM UTF-8 file I/O: titles with accents broke the default cp1252 reads.
set PYTHONUTF8=1
set WHISPER_DEVICE=cpu
set WHISPER_COMPUTE=int8
set CLIP_WORKERS=2
where nvidia-smi >nul 2>&1 && (set WHISPER_DEVICE=cuda& set WHISPER_COMPUTE=float16& set FFMPEG_ENCODER=auto)
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8000
exit /b

:ui
set VITE_PROXY_TARGET=http://localhost:8000
cd dashboard && npx vite --port 5175 --strictPort
exit /b

:lineup
REM Watches every lineup.json channel; running it confirms you may clip those streams.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
.venv\Scripts\python lineup.py --i-have-rights
exit /b
