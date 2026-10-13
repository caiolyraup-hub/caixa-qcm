@echo off
rem Agenda a sincronizacao diaria no Windows (rode uma vez). Para remover: agendar_sincronizacao.bat remover
cd /d "%~dp0"
if /i "%~1"=="remover" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0agendar_sincronizacao.ps1" -Remover
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0agendar_sincronizacao.ps1"
)
pause
