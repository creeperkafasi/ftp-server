@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python client.py %*
    goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 client.py %*
    goto :end
)

echo [HATA] Python bulunamadi! Lutfen https://www.python.org adresinden Python 3 kurun.

:end
echo.
pause
