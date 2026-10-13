@echo off
setlocal
cd /d "%~dp0"
title Caixa QCM

if not exist ".venv\Scripts\python.exe" (
    echo Primeira execucao: criando ambiente Python 3.12 ...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERRO: nao foi possivel criar o ambiente. Instale o Python 3.12 em https://www.python.org/downloads/
        echo e marque "Add python.exe to PATH".
        pause
        exit /b 1
    )
)

rem Instala/atualiza dependencias apenas quando o requirements.txt mudou
fc /b requirements.txt .venv\requirements.instalado >nul 2>&1
if errorlevel 1 (
    echo Instalando dependencias ...
    ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo ERRO ao instalar dependencias. Verifique a conexao com a internet.
        pause
        exit /b 1
    )
    copy /y requirements.txt .venv\requirements.instalado >nul
)

if defined CAIXA_QCM_DEMO (
    set "CAIXA_QCM_DATA=%~dp0data_demo"
    ".venv\Scripts\python.exe" -m caixa.demo
)

echo Abrindo o Caixa QCM em http://localhost:8501  (feche esta janela para encerrar)
rem headless evita o pedido interativo de e-mail do Streamlit; o navegador e aberto aqui
if not defined CAIXA_QCM_NO_BROWSER start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process http://localhost:8501"
".venv\Scripts\python.exe" -m streamlit run app.py --server.headless true --server.address localhost --server.port 8501 --browser.gatherUsageStats false
pause
