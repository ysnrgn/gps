@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe main.py
) else (
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [HATA] Python bulunamadi. Once kur.bat calistirin.
        pause
        exit /b 1
    )
    python main.py
)
