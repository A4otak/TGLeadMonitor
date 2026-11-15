#!/usr/bin/env bash
# Запуск TGLeadMonitor на Linux / macOS: создаёт .venv, ставит зависимости, запускает main.py
set -e
cd "$(dirname "$0")"

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3.11+ не найден. Установите его и повторите запуск."
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Нужен Python 3.11 или новее (у вас: $("$PY" --version))."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Создаю виртуальное окружение .venv ..."
    "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
exec python main.py
