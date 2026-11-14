@echo off
setlocal
cd /d "%~dp0"
if not exist config.yaml copy /y config.example.yaml config.yaml >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.14 tools/selfcheck.py --no-network %*
) else (
  python tools/selfcheck.py --no-network %*
)
pause
endlocal
