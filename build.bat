@echo off
REM ---------------------------------------------------------------------------
REM Build CueForge.exe reliably.
REM
REM PyInstaller runs with --clean/--noconfirm, which deletes dist\CueForge first.
REM If a CueForge.exe is still running it locks files there and the delete fails
REM PART-WAY, leaving a corrupted bundle (e.g. the exe then serves
REM {"web":"no index.html yet"} or a "half" page when a bundled JS module 404s).
REM So we ALWAYS close any running instance before building.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo [build] Closing any running CueForge.exe...
taskkill /IM CueForge.exe /F >nul 2>&1
REM Give Windows a moment to release the file handles.
ping -n 2 127.0.0.1 >nul

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [build] Building with %PY% ...
"%PY%" build_exe.py
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [build] Done -^> dist\CueForge.exe
) else (
  echo [build] FAILED with exit code %RC%. See the output above.
)

REM Keep the window open when double-clicked; harmless when run from a terminal.
pause
endlocal
exit /b %RC%
