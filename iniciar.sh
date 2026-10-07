#!/bin/sh
# Raspberry Pi / Linux
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python app.py
