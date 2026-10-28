@echo off
REM =============================================================================
REM jamly HTTP status server — portable FOREGROUND launcher (Windows).
REM
REM Starts the queue-status HTTP server that the browser popup / Tampermonkey
REM userscript connect to. Equivalent to:
REM     python -m jamly.server --port 8765 [--scope <dir>] [--audio-format wav|mp3]
REM
REM Usage:   start-jamly-server.bat [PORT]      (PORT optional, default 8765)
REM Run from anywhere; it cd's into the repo root so `import jamly` works without
REM an editable install. Requires python on PATH and yt-dlp + ffmpeg resolvable in scope.
REM =============================================================================
setlocal
where python >nul 2>nul || (echo "python not found on PATH" & exit /b 1)
set "PYTHON=python"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

for %%I in ("%~dp0\..\..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"
set "PYTHONPATH=%REPO_ROOT%\src"
echo jamly server: starting on http://127.0.0.1:%PORT%  (scope=%CD%)
"%PYTHON%" -m jamly.server --port %PORT% --scope "%CD%"
