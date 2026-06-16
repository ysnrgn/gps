@echo off
chcp 65001 >nul
title GPS Uygulama - Kurulum
cd /d "%~dp0"

echo ============================================
echo  GPS Uygulama Kurulum
echo ============================================
echo.

:: Python kontrolü
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Python bulunamadi. Python 3.12 indiriliyor...
    echo     Bu islem birkaç dakika sürebilir.
    echo.
    powershell -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%TEMP%\python_setup.exe' -UseBasicParsing"
    if %errorlevel% neq 0 (
        echo [HATA] Python indirilemedi. Internet baglantinizi kontrol edin.
        pause
        exit /b 1
    )
    echo [*] Python kuruluyor...
    "%TEMP%\python_setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=0
    del "%TEMP%\python_setup.exe" >nul 2>&1
    :: PATH'i bu oturum için güncelle
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [HATA] Python kurulumu basarisiz. Lutfen https://python.org adresinden manuel kurun.
        pause
        exit /b 1
    )
    echo [OK] Python kuruldu.
) else (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    echo [OK] Python %PYVER% mevcut.
)

echo.

:: Sanal ortam
if not exist ".venv\Scripts\python.exe" (
    echo [*] Sanal ortam olusturuluyor...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [HATA] Sanal ortam olusturulamadi.
        pause
        exit /b 1
    )
    echo [OK] Sanal ortam olusturuldu.
) else (
    echo [OK] Sanal ortam zaten mevcut.
)

echo.

:: Paket kurulumu
echo [*] Gerekli paketler yukleniyor... (ilk kurulumda 5-10 dakika sürebilir)
.venv\Scripts\pip install -r requirements.txt --quiet --no-warn-script-location
if %errorlevel% neq 0 (
    echo [HATA] Paket kurulumu basarisiz. Internet baglantinizi kontrol edin.
    pause
    exit /b 1
)
echo [OK] Tüm paketler yuklendi.

echo.
echo ============================================
echo  Kurulum tamamlandi!
echo  Uygulamayi baslatmak icin: calistir.bat
echo ============================================
echo.
pause
