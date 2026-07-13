from __future__ import annotations

import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from bili_gui_downloader.config import APP_NAME, ensure_app_paths, load_config
from bili_gui_downloader.ui.main_window import MainWindow


PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
)


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    # 视频下载始终直连 B 站；aria2c 也会继承这里清理后的环境。
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")

    paths = ensure_app_paths()
    config = load_config(paths)
    window = MainWindow(config=config, paths=paths)
    window.run()

    auto_close_ms = os.environ.get("BILI_GUI_AUTOCLOSE_MS", "").strip()
    if auto_close_ms.isdigit():
        QTimer.singleShot(int(auto_close_ms), app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
