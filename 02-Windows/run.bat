@echo off
setlocal
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
  python -m venv .venv
)

.\.venv\Scripts\python -m pip install -r requirements.txt
set PYTHONPATH=%cd%\src
.\.venv\Scripts\python -m bili_gui_downloader.app
