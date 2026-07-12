from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


LOGIN_URL = "https://www.bilibili.com/"


@dataclass(frozen=True)
class BrowserInstall:
    browser_id: str
    display_name: str
    app_name: str
    bundle_id: str
    app_path: Path

    @property
    def executable(self) -> Path:
        return self.app_path / "Contents" / "MacOS" / self.app_name


def list_available_browsers() -> list[BrowserInstall]:
    candidates = [
        _resolve_browser_install(
            browser_id="chrome",
            display_name="Google Chrome",
            app_name="Google Chrome",
            bundle_id="com.google.Chrome",
        ),
        _resolve_browser_install(
            browser_id="edge",
            display_name="Microsoft Edge",
            app_name="Microsoft Edge",
            bundle_id="com.microsoft.edgemac",
        ),
    ]
    return [item for item in candidates if item is not None]


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
        raise RuntimeError("未检测到 Google Chrome 或 Microsoft Edge，请先安装其中一个浏览器。")

    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            "open",
            "-na",
            str(browser.app_path),
            "--args",
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
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
    if browser is None or not profile_dir.exists():
        return None

    if not _has_login_profile(profile_dir):
        return None

    return (browser.browser_id, str(profile_dir), None, None)


def _resolve_browser_install(
    browser_id: str,
    display_name: str,
    app_name: str,
    bundle_id: str,
) -> BrowserInstall | None:
    for app_path in _iter_candidate_app_paths(app_name, bundle_id):
        executable = app_path / "Contents" / "MacOS" / app_name
        if executable.exists():
            return BrowserInstall(
                browser_id=browser_id,
                display_name=display_name,
                app_name=app_name,
                bundle_id=bundle_id,
                app_path=app_path,
            )
    return None


def _iter_candidate_app_paths(app_name: str, bundle_id: str) -> list[Path]:
    app_bundle_name = f"{app_name}.app"
    candidates: list[Path] = [
        Path("/Applications") / app_bundle_name,
        Path.home() / "Applications" / app_bundle_name,
    ]

    for app_path in _find_apps_with_spotlight(bundle_id):
        if app_path not in candidates:
            candidates.append(app_path)

    return candidates


def _find_apps_with_spotlight(bundle_id: str) -> list[Path]:
    try:
        result = subprocess.run(
            ["mdfind", f'kMDItemCFBundleIdentifier == "{bundle_id}"'],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    app_paths: list[Path] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text.endswith(".app"):
            continue
        app_path = Path(text)
        if app_path.exists() and app_path not in app_paths:
            app_paths.append(app_path)
    return app_paths


def _has_login_profile(profile_dir: Path) -> bool:
    cookie_candidates = [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    ]
    cookie_candidates.extend(profile / "Cookies" for profile in profile_dir.glob("Profile *"))
    cookie_candidates.extend(profile / "Network" / "Cookies" for profile in profile_dir.glob("Profile *"))

    if any(candidate.exists() for candidate in cookie_candidates):
        return True

    local_state = profile_dir / "Local State"
    return local_state.exists() and any(profile_dir.iterdir())
