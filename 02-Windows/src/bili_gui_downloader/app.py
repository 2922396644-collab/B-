from __future__ import annotations

from bili_gui_downloader.config import ensure_app_paths, load_config
from bili_gui_downloader.ui.main_window import MainWindow


def main() -> None:
    paths = ensure_app_paths()
    config = load_config(paths)
    window = MainWindow(config=config, paths=paths)
    window.run()


if __name__ == "__main__":
    main()
