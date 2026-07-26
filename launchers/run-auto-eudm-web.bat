@echo off
setlocal
cd /d "%~dp0.."

where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%CD%\start_auto_eudm.py" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not errorlevel 1 (
  python "%CD%\start_auto_eudm.py" %*
  exit /b %ERRORLEVEL%
)

echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/, then run this file again.
exit /b 1
