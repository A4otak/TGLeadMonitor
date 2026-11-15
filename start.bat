@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=python
where py >nul 2>nul && set PY=py -3

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Nuzhen Python 3.11 ili novee. Ustanovite ego s python.org i povtorite zapusk.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Sozdayu virtualnoe okruzhenie .venv ...
    %PY% -m venv .venv
    if errorlevel 1 ( pause & exit /b 1 )
)

call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 ( pause & exit /b 1 )
python main.py
pause
