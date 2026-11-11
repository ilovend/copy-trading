@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Binance Copy WS -> Telegram
:loop
echo [%date% %time%] starting listener >> bot\data\bot.log
python bot\listen_copy.py >> bot\data\bot.log 2>&1
echo [%date% %time%] exited, restart in 5s >> bot\data\bot.log
timeout /t 5 >nul
goto loop
