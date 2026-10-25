@echo off
REM =============================================================================
REM splitfy HTTP status server — portable FOREGROUND launcher (Windows).
REM
REM Starts the queue-status HTTP server that the browser popup / Tampermonkey
REM userscript connect to. Equivalent to:
REM     python -m splitfy.server --port 8765 [--scope <dir>] [--audio-format wav|mp3]
REM
REM Usage:   start-splitfy-server.bat [PORT]      (PORT optional, default 8765)
REM Run from anywhere; it cd's into the repo root so `import splitfy` works without
REM an editable install. Requires python on PATH and yt-dlp + ffmpeg resolvable in scope.
REM =============================================================================
setlocal
where python >nul 2>nul || (echo "python not found on PATH" & exit /b 1)
set "PYTHON=python"
if "%~1"=="" set "PORT=8765"

cd /d "%~dp0\.\.."   REM repo root (two levels up from scripts\windows\) so import splitfy works
echo splitfy server: starting on http://127.0.0.1:%PORT%  (scope=%CD%)
"%PYTHON%" -m splitfy.server --port %PORT% --scope "%CD%"
