#!/bin/zsh
set -euo pipefail

cd -- "$(dirname "$0")"

VENV_DIR="$HOME/Library/Caches/BiliHighQualityDownloader/venv"
APP_NAME="B站高码流视频下载"
ICON_PNG="$PWD/assets/app_icon.png"
ICON_ICNS="$PWD/assets/app_icon.icns"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  mkdir -p "$(dirname "$VENV_DIR")"
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -r requirements.txt
"$VENV_DIR/bin/python" -m pip install -r build-requirements.txt

if [[ -f "$ICON_PNG" ]] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/bili-icon.XXXXXX")"
  iconset_dir="$tmp_dir/app_icon.iconset"
  mkdir -p "$iconset_dir"

  for size in 16 32 128 256 512; do
    retina_size=$((size * 2))
    sips -z "$size" "$size" "$ICON_PNG" --out "$iconset_dir/icon_${size}x${size}.png" >/dev/null
    sips -z "$retina_size" "$retina_size" "$ICON_PNG" --out "$iconset_dir/icon_${size}x${size}@2x.png" >/dev/null
  done

  iconutil -c icns "$iconset_dir" -o "$ICON_ICNS"
  rm -rf "$tmp_dir"
fi

export PYTHONPATH="$PWD/src"

ARIA2C_PATH="$HOME/.local/bin/aria2c"

pyinstaller_args=(
  --noconfirm
  --clean
  --windowed
  --name "$APP_NAME"
  --paths "src"
  --add-data "assets:assets"
  --collect-submodules yt_dlp
  "src/bili_gui_downloader/app.py"
)

if [[ -f "$ICON_ICNS" ]]; then
  pyinstaller_args=(--icon "$ICON_ICNS" "${pyinstaller_args[@]}")
fi

if [[ -x "$ARIA2C_PATH" ]]; then
  pyinstaller_args=(--add-binary "$ARIA2C_PATH:bin" "${pyinstaller_args[@]}")
fi

"$VENV_DIR/bin/python" -m PyInstaller "${pyinstaller_args[@]}"

echo
echo "打包完成：dist/$APP_NAME.app"
