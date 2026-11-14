@echo off
setlocal
cd /d "%~dp0"
if not exist config.yaml (
  copy /y config.example.yaml config.yaml >nul
  echo Created config.yaml from config.example.yaml.
  echo Set OPENAI_API_KEY and DEEPSEEK_API_KEY before using cloud answers.
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.14 main.py %*
) else (
  python main.py %*
)
endlocal
