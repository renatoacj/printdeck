@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Criando ambiente Python...
  python -m venv .venv || (echo Instale o Python em https://www.python.org/downloads/ & pause & exit /b 1)
)
.venv\Scripts\pip install -q --disable-pip-version-check -r requirements.txt
echo.
echo  Abrindo em http://localhost:5000
echo  Para liberar aos amigos pela internet: Administrativo ^> "Ligar acesso pela internet"
echo  (feche esta janela para desligar)
echo.
start "" http://localhost:5000
.venv\Scripts\python app.py
if errorlevel 1 pause
