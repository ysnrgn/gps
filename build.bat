@echo off
chcp 65001 >nul
title GPS Uygulama - Build
cd /d "%~dp0"

echo ============================================
echo  GPS Uygulama - PyInstaller Build
echo ============================================
echo.

:: Sanal ortam ve PyInstaller kontrolü
if not exist ".venv\Scripts\pyinstaller.exe" (
    echo [*] PyInstaller yukleniyor...
    .venv\Scripts\pip install pyinstaller --quiet
)

echo [*] Eski build temizleniyor...
if exist "build" rmdir /s /q build
if exist "dist\GPS_App" rmdir /s /q "dist\GPS_App"

echo.
echo [*] Derleme basliyor... (5-15 dakika sürebilir)
echo.
.venv\Scripts\pyinstaller GPS_App.spec --clean --noconfirm

if %errorlevel% neq 0 (
    echo.
    echo [HATA] Derleme basarisiz. Yukaridaki hata mesajini inceleyin.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Derleme tamamlandi!
echo.
echo  Cikti: dist\GPS_App\GPS_App.exe
echo.
echo  Dagitim icin:
echo    1. dist\GPS_App\ klasorunu kopyalayin
echo    2. Haritalari kullanmak icin map_data\ klasorunu
echo       GPS_App\ klasorune kopyalayin
echo ============================================
echo.
pause
