@echo off
rem Sincroniza a Conta Simples agora e mostra o que falta (sem abrir o app). Log em data\sync.log
cd /d "%~dp0"
".venv\Scripts\python.exe" -m caixa.sync %*
pause
