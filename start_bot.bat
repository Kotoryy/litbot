@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
:loop
python bot.py
echo Bot stopped. Restarting in 30 seconds... (Ctrl+C to exit)
timeout /t 30 >nul
goto loop
