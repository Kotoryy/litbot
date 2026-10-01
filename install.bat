@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Установка бота LitBot

echo ============================================
echo   Установка бота LitBot
echo ============================================
echo.

rem --- 1. Ищем Python 3.10+ -------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo [!] Python 3.10 или новее не найден.
    echo.
    where winget >nul 2>&1
    if not errorlevel 1 (
        echo Сейчас будет установлен Python 3.12 через winget.
        echo Если Windows спросит разрешение — подтвердите.
        echo.
        winget install -e --id Python.Python.3.12
        echo.
        echo Python установлен. ЗАКРОЙТЕ это окно и запустите install.bat ещё раз.
    ) else (
        echo Установите Python вручную: откроется страница загрузки.
        echo ВАЖНО: в установщике отметьте галочку "Add python.exe to PATH".
        echo После установки запустите install.bat ещё раз.
        start "" "https://www.python.org/downloads/"
    )
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('%PY% --version') do echo [ok] Найден %%v

rem --- 2. Отдельное окружение для бота ---------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [!] Не удалось создать окружение.
        pause
        exit /b 1
    )
)
echo [ok] Окружение .venv готово

rem --- 3. Пакеты ---------------------------------------------------------------
echo Устанавливаю пакеты (1-2 минуты) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo [!] Пакеты не установились. Проверьте интернет и запустите install.bat ещё раз.
    pause
    exit /b 1
)
echo [ok] Пакеты установлены

rem --- 4. Проверка, что бот запускается ----------------------------------------
".venv\Scripts\python.exe" -c "import bot, litparser, registry, yaml; yaml.safe_load(open('config.yaml', encoding='utf-8'))"
if errorlevel 1 (
    echo [!] Бот не запускается — пришлите текст ошибки выше.
    pause
    exit /b 1
)
echo [ok] Бот и config.yaml в порядке

rem --- 5. Токен ------------------------------------------------------------------
if not exist "token.txt" type nul > token.txt
for %%f in (token.txt) do if %%~zf==0 (
    echo.
    echo [!] Файл token.txt пустой: вставьте в него токен бота от @BotFather и сохраните.
)

echo.
echo ============================================
echo   Готово! Запуск бота: start_bot.bat
echo ============================================
echo.
echo Не запускайте бота одновременно на двух компьютерах:
echo сначала остановите его на старом.
echo.
pause
