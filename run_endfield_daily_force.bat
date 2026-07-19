@echo off
chcp 65001 >nul
cd /d "%~dp0"
python main.py run endfield_daily --force
echo.
pause
