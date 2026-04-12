#!/bin/zsh
set -euo pipefail

cd -- "$(dirname "$0")"

VENV_DIR="$HOME/Library/Caches/BiliHighQualityDownloader/venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  mkdir -p "$(dirname "$VENV_DIR")"
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -r requirements.txt
export PYTHONPATH="$PWD/src"
exec "$VENV_DIR/bin/python" -m bili_gui_downloader.app
