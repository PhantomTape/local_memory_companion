@echo off
setlocal
if "%OPENAI_API_KEY%"=="" (
  echo OPENAI_API_KEY is not set.
  echo In PowerShell, run:
  echo $env:OPENAI_API_KEY="your_api_key_here"
  pause
  exit /b 1
)
python app.py
pause
