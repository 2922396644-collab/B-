from __future__ import annotations

import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from bili_gui_downloader.config import ensure_app_paths, load_config
from bili_gui_downloader.ui.main_window import MainWindow


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("B站高码流视频下载")
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
