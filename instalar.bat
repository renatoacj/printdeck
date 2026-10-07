@echo off
rem Instalador do PrintDeck para Windows: prepara tudo e cria o atalho na Area de Trabalho.
rem Uso: dois cliques neste arquivo.   (instalar.bat /teste = so prepara, sem criar atalho nem abrir)
title Instalando o PrintDeck
cd /d "%~dp0"
echo.
echo   ==========================================
echo    PrintDeck - instalacao
echo   ==========================================
echo.

rem --- 1. Python: procura no caminho do Windows, no lancador "py" e na pasta padrao de instalacao
rem     (o "python" da Microsoft Store sem nada instalado so abre a loja; por isso o teste de verdade)
set "TESTE_PY=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
set "PY="
python -c "%TESTE_PY%" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "%TESTE_PY%" >nul 2>nul && set "PY=py -3"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~D\python.exe" set PY="%%~D\python.exe"
if not defined PY (
  echo   [1/4] Python 3.10 ou mais novo nao foi encontrado. Instalando o Python 3.12...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (
    echo.
    echo   Nao consegui instalar sozinho. Baixe o Python em https://www.python.org/downloads/
    echo   ^(marque "Add python.exe to PATH"^), instale e abra o instalar.bat de novo.
  ) else (
    echo.
    echo   Python instalado. FECHE esta janela e abra o instalar.bat de novo para continuar.
  )
  echo.
  pause
  exit /b 0
)
echo   [1/4] Python encontrado.

rem --- 2. ambiente proprio do PrintDeck (nao mexe no Python do computador)
if not exist ".venv\Scripts\python.exe" (
  echo   [2/4] Criando o ambiente do PrintDeck...
  %PY% -m venv .venv
  if errorlevel 1 goto erro
) else (
  echo   [2/4] Ambiente do PrintDeck ja existe.
)

rem --- 3. dependencias
echo   [3/4] Instalando as dependencias ^(pode levar de 1 a 3 minutos^)...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto erro

if /i "%~1"=="/teste" (
  echo   [4/4] Modo de teste: atalho e abertura pulados.
  echo.
  echo   OK
  exit /b 0
)

rem --- 4. atalhos (Area de Trabalho e menu Iniciar)
echo   [4/4] Criando o atalho "PrintDeck"...
".venv\Scripts\python.exe" desktop.py --atalho
if errorlevel 1 goto erro

echo.
echo   ==========================================
echo    Pronto! O PrintDeck foi instalado.
echo   ==========================================
echo.
echo   - Abra pelo atalho "PrintDeck" na Area de Trabalho ^(vai abrir agora^).
echo   - Senha inicial do Administrativo: admin  ^(troque em Configuracoes^).
echo   - NAO mova nem apague esta pasta: o atalho aponta para ela.
echo.
where cloudflared >nul 2>nul
if errorlevel 1 if not exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" if not exist "%ProgramFiles%\Tailscale\tailscale.exe" (
  echo   Para os amigos acessarem pela internet, instale UM dos dois ^(veja o README^):
  echo     winget install Cloudflare.cloudflared      ^(link que muda, sem conta^)
  echo     https://tailscale.com/download             ^(link fixo, conta gratis^)
  echo.
)
start "" ".venv\Scripts\pythonw.exe" desktop.py
pause
exit /b 0

:erro
echo.
echo   Algo deu errado na etapa acima. Confira a mensagem, sua internet e tente de novo.
echo.
pause
exit /b 1
