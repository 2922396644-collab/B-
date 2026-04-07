from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


LOGIN_URL = "https://www.bilibili.com/"


@dataclass(frozen=True, slots=True)
class BrowserInstall:
    browser_id: str
    display_name: str
    executable: Path


def list_available_browsers() -> list[BrowserInstall]:
    candidates = [
        BrowserInstall(
            browser_id="chrome",
            display_name="Google Chrome",
            executable=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ),
        BrowserInstall(
            browser_id="chrome",
            display_name="Google Chrome",
            executable=Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ),
        BrowserInstall(
            browser_id="edge",
            display_name="Microsoft Edge",
            executable=Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ),
        BrowserInstall(
            browser_id="edge",
            display_name="Microsoft Edge",
            executable=Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ),
    ]
    return [item for item in candidates if item.executable.exists()]


def resolve_browser(preference: str) -> BrowserInstall | None:
    available = list_available_browsers()
    if not available:
        return None

    if preference and preference != "auto":
        for item in available:
            if item.browser_id == preference:
                return item

    for preferred in ("chrome", "edge"):
        for item in available:
            if item.browser_id == preferred:
                return item
    return available[0]


def launch_login_browser(profile_dir: Path, preference: str) -> BrowserInstall:
    browser = resolve_browser(preference)
    if browser is None:
        raise RuntimeError("未检测到 Chrome 或 Edge，请先安装其中一个浏览器。")

    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(browser.executable),
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--new-window",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return browser


def build_cookies_from_browser(profile_dir: Path, preference: str) -> tuple[str, str, None, None] | None:
    profile_dir = profile_dir.resolve()
    browser = resolve_browser(preference)
    if browser is None:
        return None

    if not profile_dir.exists():
        return None

    if not any(profile_dir.iterdir()):
        return None

    return (browser.browser_id, str(profile_dir), None, None)
