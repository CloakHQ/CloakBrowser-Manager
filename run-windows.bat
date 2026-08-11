@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run.py
) else (
    python run.py
)

if not %errorlevel%==0 pause
