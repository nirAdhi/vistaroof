@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist .env (
  echo No .env file found - copying .env.example. Edit .env and add your ANTHROPIC_API_KEY.
  copy .env.example .env >nul
)
echo.
echo Roofing Ledger running at http://127.0.0.1:8000  (Ctrl+C to stop)
echo.
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
