@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LitBot

if not exist ".venv\Scripts\python.exe" (
    echo Бот ещё не установлен на этом компьютере: сначала запустите install.bat
    pause
    exit /b 1
)

:loop
".venv\Scripts\python.exe" bot.py
echo.
echo Бот остановился. Перезапуск через 30 секунд... (закройте окно, чтобы выйти)
timeout /t 30 >nul
goto loop
