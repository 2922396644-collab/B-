from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


APP_NAME = "B站高码流视频下载"
APP_DIR_NAME = "BiliHighQualityDownloader"


@dataclass
class AppPaths:
    root_dir: Path
    data_dir: Path
    logs_dir: Path
    temp_dir: Path
    browser_profile_dir: Path
    config_path: Path
    history_path: Path


@dataclass
class AppConfig:
    default_download_dir: str = ""
    browser_preference: str = "auto"
    concurrent_downloads: int = 2
    concurrent_fragments: int = 4
    theme_mode: str = "system"
    theme_preference_explicit: bool = False

    def clamp(self) -> "AppConfig":
        self.concurrent_downloads = max(1, min(4, int(self.concurrent_downloads)))
        self.concurrent_fragments = max(1, min(8, int(self.concurrent_fragments)))
        self.browser_preference = self.browser_preference or "auto"
        self.default_download_dir = self.default_download_dir.strip()
        if self.theme_mode not in {"light", "dark", "system"}:
            self.theme_mode = "system"
        return self


def get_app_root() -> Path:
    return (Path.home() / "Library" / "Application Support" / APP_DIR_NAME).resolve()


def ensure_app_paths() -> AppPaths:
    root_dir = get_app_root()
    data_dir = root_dir / "data"
    logs_dir = root_dir / "logs"
    temp_dir = data_dir / "temp"
    browser_profile_dir = data_dir / "browser_profile"
    config_path = root_dir / "config.json"
    history_path = data_dir / "download_history.json"

    for path in (root_dir, data_dir, logs_dir, temp_dir, browser_profile_dir):
        path.mkdir(parents=True, exist_ok=True)

    return AppPaths(
        root_dir=root_dir,
        data_dir=data_dir,
        logs_dir=logs_dir,
        temp_dir=temp_dir,
        browser_profile_dir=browser_profile_dir,
        config_path=config_path,
        history_path=history_path,
    )


def load_config(paths: AppPaths) -> AppConfig:
    if not paths.config_path.exists():
        config = AppConfig().clamp()
        save_config(paths, config)
        return config

    try:
        loaded = json.loads(paths.config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config is not an object")
    except Exception:
        loaded = {}

    config = AppConfig(
        default_download_dir=str(loaded.get("default_download_dir", "")),
        browser_preference=str(loaded.get("browser_preference", "auto")),
        concurrent_downloads=int(loaded.get("concurrent_downloads", 2)),
        concurrent_fragments=int(loaded.get("concurrent_fragments", 4)),
        theme_mode=str(loaded.get("theme_mode", "system")),
        theme_preference_explicit=bool(loaded.get("theme_preference_explicit", False)),
    ).clamp()

    if not config.theme_preference_explicit and config.theme_mode == "light":
        config.theme_mode = "system"

    if config.default_download_dir:
        config.default_download_dir = str(Path(config.default_download_dir).resolve())

    save_config(paths, config)
    return config


def save_config(paths: AppPaths, config: AppConfig) -> None:
    paths.config_path.write_text(
        json.dumps(asdict(config.clamp()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
