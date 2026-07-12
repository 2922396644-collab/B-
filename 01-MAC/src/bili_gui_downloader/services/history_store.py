from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from bili_gui_downloader.config import AppPaths


MAX_HISTORY_ITEMS = 1000


@dataclass
class DownloadHistoryEntry:
    recorded_at: str
    title: str
    uploader: str
    video_id: str
    source_url: str
    quality_text: str
    duration_text: str
    status: str
    output_dir: str
    output_path: str = ""


def load_history(paths: AppPaths) -> list[DownloadHistoryEntry]:
    history_path = paths.history_path
    if not history_path.exists():
        return []

    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("history is not a list")
    except Exception:
        return []

    entries: list[DownloadHistoryEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        entries.append(
            DownloadHistoryEntry(
                recorded_at=str(item.get("recorded_at", "")),
                title=str(item.get("title", "")),
                uploader=str(item.get("uploader", "")),
                video_id=str(item.get("video_id", "")),
                source_url=str(item.get("source_url", "")),
                quality_text=str(item.get("quality_text", "")),
                duration_text=str(item.get("duration_text", "")),
                status=str(item.get("status", "")),
                output_dir=str(item.get("output_dir", "")),
                output_path=str(item.get("output_path", "")),
            )
        )
    return entries


def append_history_entries(paths: AppPaths, entries: list[DownloadHistoryEntry]) -> None:
    if not entries:
        return

    existing = load_history(paths)
    combined = entries + existing
    save_history(paths, combined[:MAX_HISTORY_ITEMS])


def clear_history(paths: AppPaths) -> None:
    save_history(paths, [])


def save_history(paths: AppPaths, entries: list[DownloadHistoryEntry]) -> None:
    history_path = paths.history_path
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_existing_path(path_text: str) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    return path if path.exists() else None
