#!/bin/sh
# Запуск на macOS / Linux
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: подготовка..."
  python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt
fi
exec .venv/bin/python app.py
