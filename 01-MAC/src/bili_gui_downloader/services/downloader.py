from __future__ import annotations

import math
import os
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bili_gui_downloader.config import AppConfig, AppPaths
from bili_gui_downloader.models import DownloadSummary, VideoMetadata
from bili_gui_downloader.services.browser_launcher import build_cookies_from_browser


class DownloadCancelled(Exception):
    pass


FORMAT_SELECTOR = "bestvideo*+bestaudio/best"
FORMAT_SORT_FIELDS = ["res", "fps", "vbr", "tbr", "abr"]
NETWORK_SOCKET_TIMEOUT_SECONDS = 30
EXTRACTOR_RETRIES = 3
DOWNLOAD_RETRIES = 8
FRAGMENT_RETRIES = 8
FILE_ACCESS_RETRIES = 3
MAX_TOTAL_FRAGMENT_DOWNLOADS = 8
PROGRESS_EMIT_INTERVAL_SECONDS = 0.25
PROGRESS_EMIT_DELTA_PERCENT = 0.4
PROGRESS_EMIT_DELTA_BYTES = 4 * 1024 * 1024


class YtDlpLogger:
    def __init__(self, log_callback: Callable[[str], None] | None) -> None:
        self.log_callback = log_callback

    def debug(self, message: str) -> None:
        if self.log_callback and message.startswith("ERROR:"):
            self.log_callback(message)

    def warning(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(f"警告：{message}")

    def error(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(f"错误：{message}")


class BiliDownloader:
    def __init__(
        self,
        config: AppConfig,
        paths: AppPaths,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.log_callback = log_callback

    def fetch_metadata_batch(self, urls: list[str]) -> list[VideoMetadata]:
        if not urls:
            return []

        cookie_spec = self._resolve_cookie_spec()
        max_workers = min(2 if cookie_spec else 4, max(1, len(urls)))
        self._log(f"读取任务启动：{len(urls)} 个链接，元数据并发 {max_workers}。")
        results: dict[int, list[VideoMetadata]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._fetch_single_metadata, index, url, cookie_spec): index
                for index, url in enumerate(urls)
            }
            for future in as_completed(futures):
                items = future.result()
                results[futures[future]] = items

        flattened: list[VideoMetadata] = []
        for index in sorted(results):
            flattened.extend(results[index])
        for output_index, item in enumerate(flattened):
            item.input_index = output_index
        return flattened

    def _fetch_single_metadata(
        self,
        index: int,
        url: str,
        cookie_spec: tuple[str, str, None, None] | None,
    ) -> list[VideoMetadata]:
        try:
            with YoutubeDL(self._build_extract_options(cookie_spec)) as ydl:
                info = ydl.extract_info(url, download=False)

            entries = info.get("entries") or []
            if entries:
                items = [
                    self._make_metadata(index, url, entry)
                    for entry in entries
                    if isinstance(entry, dict)
                ]
                if items:
                    self._log(f"检测到合集链接，已展开 {len(items)} 个视频。")
                    return items
            return [self._make_metadata(index, url, info)]
        except Exception as exc:
            return [
                VideoMetadata(
                    task_id=uuid.uuid4().hex,
                    input_index=index,
                    source_url=url,
                    normalized_url=url,
                    title="读取失败",
                    uploader="--",
                    duration_text="--",
                    best_quality_text="--",
                    status="读取失败",
                    error_message=_friendly_error(exc),
                )
            ]

    @staticmethod
    def _make_metadata(index: int, source_url: str, info: dict) -> VideoMetadata:
        component_estimated_bytes = _extract_component_sizes(info)
        return VideoMetadata(
            task_id=uuid.uuid4().hex,
            input_index=index,
            source_url=source_url,
            normalized_url=info.get("webpage_url") or source_url,
            title=str(info.get("title") or "未命名视频"),
            uploader=str(info.get("uploader") or info.get("channel") or "未知作者"),
            duration_text=_format_duration(info.get("duration")),
            best_quality_text=_describe_best_quality(info),
            video_id=str(info.get("id") or ""),
            status="待下载",
            estimated_total_bytes=sum(component_estimated_bytes.values()),
            component_estimated_bytes=component_estimated_bytes,
        )

    def download_batch(
        self,
        items: list[VideoMetadata],
        output_dir: Path,
        stop_event: threading.Event,
        item_callback: Callable[[str, dict], None] | None = None,
    ) -> DownloadSummary:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        valid_items = [item for item in items if not item.error_message]
        summary = DownloadSummary(total_count=len(valid_items), final_output_dir=str(output_dir))
        if not valid_items:
            return summary

        max_workers = min(self.config.concurrent_downloads, max(1, len(valid_items)))
        fragment_downloads = _effective_fragment_downloads(
            configured=self.config.concurrent_fragments,
            active_downloads=max_workers,
        )
        cookie_spec = self._resolve_cookie_spec()
        self._log(
            f"下载任务启动：{len(valid_items)} 个视频，同时下载 {max_workers} 个，"
            f"单任务分片并发 {fragment_downloads} 个。"
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._download_single,
                    item,
                    output_dir,
                    stop_event,
                    item_callback,
                    fragment_downloads,
                    cookie_spec,
                ): item.task_id
                for item in valid_items
            }

            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result == "success":
                        summary.success_count += 1
                    elif result == "stopped":
                        summary.stopped_count += 1
                    else:
                        summary.failed_count += 1
                except Exception:
                    summary.failed_count += 1

        return summary

    def _download_single(
        self,
        item: VideoMetadata,
        output_dir: Path,
        stop_event: threading.Event,
        item_callback: Callable[[str, dict], None] | None,
        fragment_downloads: int,
        cookie_spec: tuple[str, str, None, None] | None,
    ) -> str:
        download_state = _build_download_state(item)
        task_temp_dir = self.paths.temp_dir / item.task_id
        task_temp_dir.mkdir(parents=True, exist_ok=True)
        if stop_event.is_set():
            self._emit_item(
                item_callback,
                item.task_id,
                status="已停止",
                detail="任务已取消",
                progress=0.0,
                downloaded_text=_format_progress_bytes(0, download_state["total_estimated"]),
                speed_text="",
                speed_bps=0.0,
                eta_text="",
            )
            return "stopped"

        self._emit_item(
            item_callback,
            item.task_id,
            status="准备下载",
            detail="正在申请下载资源",
            progress=0.0,
            downloaded_text=_format_progress_bytes(0, download_state["total_estimated"]),
            speed_text="",
            speed_bps=0.0,
            eta_text="",
        )
        progress_hook = self._build_progress_hook(item.task_id, stop_event, item_callback, download_state)

        try:
            with YoutubeDL(
                self._build_download_options(
                    output_dir=output_dir,
                    progress_hook=progress_hook,
                    temp_dir=task_temp_dir,
                    fragment_downloads=fragment_downloads,
                    cookie_spec=cookie_spec,
                )
            ) as ydl:
                ydl.extract_info(item.normalized_url, download=True)

            total_downloaded = _get_completed_bytes(download_state)
            total_estimated = _get_total_estimated(download_state)
            self._emit_item(
                item_callback,
                item.task_id,
                status="已完成",
                progress=100.0,
                detail="下载完成",
                downloaded_text=_format_progress_bytes(total_downloaded or total_estimated, total_estimated),
                speed_text="",
                speed_bps=0.0,
                eta_text="",
            )
            return "success"
        except DownloadCancelled:
            self._emit_item(
                item_callback,
                item.task_id,
                status="已停止",
                detail="用户手动停止了任务",
                progress=_get_progress_percent(download_state),
                downloaded_text=_format_progress_bytes(
                    _get_overall_downloaded(download_state),
                    _get_total_estimated(download_state),
                ),
                speed_text="",
                speed_bps=0.0,
                eta_text="",
            )
            return "stopped"
        except DownloadError as exc:
            if stop_event.is_set():
                self._emit_item(
                    item_callback,
                    item.task_id,
                    status="已停止",
                    detail="用户手动停止了任务",
                    progress=_get_progress_percent(download_state),
                    downloaded_text=_format_progress_bytes(
                        _get_overall_downloaded(download_state),
                        _get_total_estimated(download_state),
                    ),
                    speed_text="",
                    speed_bps=0.0,
                    eta_text="",
                )
                return "stopped"

            self._emit_item(
                item_callback,
                item.task_id,
                status="失败",
                detail=_friendly_error(exc),
                progress=_get_progress_percent(download_state),
                downloaded_text=_format_progress_bytes(
                    _get_overall_downloaded(download_state),
                    _get_total_estimated(download_state),
                ),
                speed_text="",
                speed_bps=0.0,
                eta_text="",
            )
            return "failed"
        except Exception as exc:
            if stop_event.is_set():
                self._emit_item(
                    item_callback,
                    item.task_id,
                    status="已停止",
                    detail="用户手动停止了任务",
                    progress=_get_progress_percent(download_state),
                    downloaded_text=_format_progress_bytes(
                        _get_overall_downloaded(download_state),
                        _get_total_estimated(download_state),
                    ),
                    speed_text="",
                    speed_bps=0.0,
                    eta_text="",
                )
                return "stopped"

            self._emit_item(
                item_callback,
                item.task_id,
                status="失败",
                detail=_friendly_error(exc),
                progress=_get_progress_percent(download_state),
                downloaded_text=_format_progress_bytes(
                    _get_overall_downloaded(download_state),
                    _get_total_estimated(download_state),
                ),
                speed_text="",
                speed_bps=0.0,
                eta_text="",
            )
            return "failed"

    def _build_progress_hook(
        self,
        task_id: str,
        stop_event: threading.Event,
        item_callback: Callable[[str, dict], None] | None,
        download_state: dict[str, object],
    ) -> Callable[[dict], None]:
        def _hook(status: dict) -> None:
            if stop_event.is_set():
                raise DownloadCancelled("stop requested")

            phase = status.get("status")
            component_id = _resolve_component_id(status)
            if phase == "downloading":
                downloaded = int(status.get("downloaded_bytes") or 0)
                total = int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
                _record_component_progress(download_state, component_id, downloaded, total)
                progress = _get_progress_percent(download_state, current_component_id=component_id)
                overall_downloaded = _get_overall_downloaded(download_state, current_component_id=component_id)
                detail = _describe_stream_phase(status.get("info_dict") or {})
                speed_bps = float(status.get("speed") or 0.0)
                payload = {
                    "status": "下载中",
                    "progress": progress,
                    "detail": detail,
                    "downloaded_text": _format_progress_bytes(overall_downloaded, _get_total_estimated(download_state)),
                    "speed_text": _format_speed(speed_bps),
                    "speed_bps": speed_bps,
                    "eta_text": _format_eta(status.get("eta")),
                    "eta_seconds": _to_int(status.get("eta")),
                }
                if _should_emit_progress(
                    download_state,
                    status="下载中",
                    progress=progress,
                    downloaded_bytes=overall_downloaded,
                ):
                    self._emit_item(item_callback, task_id, **payload)
            elif phase == "finished":
                _mark_component_finished(download_state, component_id)
                finished_count = len(download_state["finished_components"])
                total_count = max(1, len(download_state["component_sizes"]))
                is_merging = finished_count >= total_count
                self._emit_item(
                    item_callback,
                    task_id,
                    status="合并音视频" if is_merging else "下载中",
                    progress=max(99.0, _get_progress_percent(download_state))
                    if is_merging
                    else _get_progress_percent(download_state),
                    detail="所有流下载完成，正在合并音视频"
                    if is_merging
                    else "一个流已完成，继续下载剩余流",
                    downloaded_text=_format_progress_bytes(
                        _get_completed_bytes(download_state),
                        _get_total_estimated(download_state),
                    ),
                    speed_text="",
                    speed_bps=0.0,
                    eta_text="",
                )

        return _hook

    def _build_extract_options(self, cookie_spec: tuple[str, str, None, None] | None) -> dict:
        options = {
            "ignoreconfig": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            # B 站下载直连，不继承桌面环境中的 HTTP(S)_PROXY / ALL_PROXY。
            "proxy": "",
            "skip_download": True,
            "ignoreerrors": False,
            "socket_timeout": NETWORK_SOCKET_TIMEOUT_SECONDS,
            "extractor_retries": EXTRACTOR_RETRIES,
            "format": FORMAT_SELECTOR,
            "format_sort": FORMAT_SORT_FIELDS,
            "logger": YtDlpLogger(self.log_callback),
        }
        if cookie_spec:
            options["cookiesfrombrowser"] = cookie_spec
        return options

    def _build_download_options(
        self,
        output_dir: Path,
        progress_hook: Callable[[dict], None],
        temp_dir: Path,
        fragment_downloads: int,
        cookie_spec: tuple[str, str, None, None] | None,
    ) -> dict:
        options = {
            "ignoreconfig": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # 直连 B 站，避免本机代理的带宽和分片重试成为下载瓶颈。
            "proxy": "",
            "ignoreerrors": False,
            "logger": YtDlpLogger(self.log_callback),
            "format": FORMAT_SELECTOR,
            "format_sort": FORMAT_SORT_FIELDS,
            "merge_output_format": "mp4",
            "windowsfilenames": True,
            "retries": DOWNLOAD_RETRIES,
            "fragment_retries": FRAGMENT_RETRIES,
            "extractor_retries": EXTRACTOR_RETRIES,
            "file_access_retries": FILE_ACCESS_RETRIES,
            "socket_timeout": NETWORK_SOCKET_TIMEOUT_SECONDS,
            "continuedl": True,
            "concurrent_fragment_downloads": fragment_downloads,
            "paths": {
                "home": str(output_dir),
                "temp": str(temp_dir),
            },
            "outtmpl": {
                "default": "%(title).150B [%(id)s].%(ext)s",
            },
            "progress_hooks": [progress_hook],
        }
        ffmpeg_location = resolve_ffmpeg_location()
        if ffmpeg_location is not None:
            # imageio-ffmpeg 的内置文件名并不一定就叫 “ffmpeg”，必须传递完整
            # 可执行文件路径；只传目录会让 yt-dlp 继续查找不存在的 ffmpeg 文件。
            options["ffmpeg_location"] = str(ffmpeg_location)
        cookie_spec = build_cookies_from_browser(
            self.paths.browser_profile_dir,
            self.config.browser_preference,
        )
        if cookie_spec:
            options["cookiesfrombrowser"] = cookie_spec
        return options

    def _resolve_cookie_spec(self) -> tuple[str, str, None, None] | None:
        return build_cookies_from_browser(
            self.paths.browser_profile_dir,
            self.config.browser_preference,
        )

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _emit_item(
        item_callback: Callable[[str, dict], None] | None,
        task_id: str,
        **payload: object,
    ) -> None:
        if item_callback:
            item_callback(task_id, dict(payload))


def is_ffmpeg_available() -> bool:
    return resolve_ffmpeg_location() is not None


def resolve_ffmpeg_location() -> Path | None:
    env_candidates = [
        os.environ.get("FFMPEG"),
        os.environ.get("FFMPEG_BINARY"),
        os.environ.get("FFMPEG_LOCATION"),
    ]
    for candidate in env_candidates:
        resolved = _normalize_ffmpeg_candidate(candidate)
        if resolved is not None:
            return resolved

    which_path = shutil.which("ffmpeg")
    resolved = _normalize_ffmpeg_candidate(which_path)
    if resolved is not None:
        return resolved

    candidate_paths = [
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
        Path("/opt/local/bin/ffmpeg"),
        Path("/usr/bin/ffmpeg"),
    ]
    for candidate in candidate_paths:
        resolved = _normalize_ffmpeg_candidate(candidate)
        if resolved is not None:
            return resolved

    # PyInstaller 将 imageio-ffmpeg 的二进制文件放到 App 的 Frameworks 目录，
    # 这时 imageio_ffmpeg.get_ffmpeg_exe() 可能无法自行定位，需要主动查找。
    app_contents_dir = Path(sys.executable).resolve().parent.parent
    bundled_ffmpeg_dir = app_contents_dir / "Frameworks" / "imageio_ffmpeg" / "binaries"
    if bundled_ffmpeg_dir.is_dir():
        for candidate in bundled_ffmpeg_dir.glob("ffmpeg*"):
            resolved = _normalize_ffmpeg_candidate(candidate)
            if resolved is not None:
                return resolved

    try:
        import imageio_ffmpeg

        resolved = _normalize_ffmpeg_candidate(imageio_ffmpeg.get_ffmpeg_exe())
        if resolved is not None:
            return resolved
    except Exception:
        pass
    return None


def _normalize_ffmpeg_candidate(value: str | Path | None) -> Path | None:
    if not value:
        return None

    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "ffmpeg"
    if path.exists() and path.is_file():
        return path.resolve()
    return None


def _describe_best_quality(info: dict) -> str:
    formats = _preferred_formats_for_display(info)
    if not formats:
        return "未知规格"

    best = max(
        formats,
        key=lambda item: (
            int(item.get("height") or 0),
            float(item.get("fps") or 0.0),
            float(item.get("vbr") or item.get("tbr") or 0.0),
            float(item.get("tbr") or 0.0),
            float(item.get("filesize") or item.get("filesize_approx") or 0.0),
        ),
    )

    parts = []
    resolution = best.get("resolution")
    if resolution:
        parts.append(str(resolution))
    fps = best.get("fps")
    if fps:
        parts.append(f"{float(fps):g}fps")
    bitrate = best.get("vbr") or best.get("tbr")
    if bitrate:
        parts.append(f"{float(bitrate):g}kbps")
    ext = best.get("ext")
    if ext:
        parts.append(str(ext))
    return " / ".join(parts) if parts else "未知规格"


def _preferred_formats_for_display(info: dict) -> list[dict]:
    requested_formats = info.get("requested_formats") or []
    if isinstance(requested_formats, list) and requested_formats:
        return [item for item in requested_formats if isinstance(item, dict)]

    requested_downloads = info.get("requested_downloads") or []
    flattened: list[dict] = []
    if isinstance(requested_downloads, list):
        for entry in requested_downloads:
            if not isinstance(entry, dict):
                continue
            inner_formats = entry.get("requested_formats") or []
            if isinstance(inner_formats, list):
                flattened.extend(item for item in inner_formats if isinstance(item, dict))
            elif entry:
                flattened.append(entry)
    if flattened:
        return flattened

    formats = info.get("formats") or []
    return [item for item in formats if isinstance(item, dict)]


def _extract_component_sizes(info: dict) -> dict[str, int]:
    requested_formats = info.get("requested_formats") or []
    if requested_formats:
        component_sizes: dict[str, int] = {}
        for component in requested_formats:
            component_id = str(component.get("format_id") or "")
            if not component_id:
                continue
            component_sizes[component_id] = _pick_size(component)
        return component_sizes

    format_id = str(info.get("format_id") or "")
    if not format_id:
        return {}
    return {format_id: _pick_size(info)}


def _pick_size(info: dict) -> int:
    return int(info.get("filesize") or info.get("filesize_approx") or 0)


def _build_download_state(item: VideoMetadata) -> dict[str, object]:
    component_sizes = {
        component_id: max(0, int(size))
        for component_id, size in item.component_estimated_bytes.items()
    }
    total_estimated = max(int(item.estimated_total_bytes or 0), sum(component_sizes.values()))
    return {
        "component_sizes": component_sizes,
        "component_downloaded": {},
        "finished_components": set(),
        "total_estimated": total_estimated,
        "last_progress": 0.0,
        "last_emit_at": 0.0,
        "last_emitted_status": "",
        "last_emitted_progress": -1.0,
        "last_emitted_downloaded": 0,
    }


def _effective_fragment_downloads(configured: int, active_downloads: int) -> int:
    per_download_cap = max(1, MAX_TOTAL_FRAGMENT_DOWNLOADS // max(1, active_downloads))
    return max(1, min(int(configured), per_download_cap))


def _should_emit_progress(
    download_state: dict[str, object],
    status: str,
    progress: float,
    downloaded_bytes: int,
) -> bool:
    now = time.monotonic()
    last_emit_at = float(download_state.get("last_emit_at") or 0.0)
    last_status = str(download_state.get("last_emitted_status") or "")
    last_progress = float(download_state.get("last_emitted_progress") or -1.0)
    last_downloaded = int(download_state.get("last_emitted_downloaded") or 0)

    should_emit = (
        status != last_status
        or progress >= 100.0
        or progress - last_progress >= PROGRESS_EMIT_DELTA_PERCENT
        or downloaded_bytes - last_downloaded >= PROGRESS_EMIT_DELTA_BYTES
        or now - last_emit_at >= PROGRESS_EMIT_INTERVAL_SECONDS
    )
    if not should_emit:
        return False

    download_state["last_emit_at"] = now
    download_state["last_emitted_status"] = status
    download_state["last_emitted_progress"] = progress
    download_state["last_emitted_downloaded"] = downloaded_bytes
    return True


def _resolve_component_id(status: dict) -> str:
    info = status.get("info_dict") or {}
    component_id = str(info.get("format_id") or "").strip()
    if component_id:
        return component_id
    filename = str(status.get("filename") or "").strip()
    return filename or "default"


def _record_component_progress(
    download_state: dict[str, object],
    component_id: str,
    downloaded: int,
    total: int,
) -> None:
    component_sizes: dict[str, int] = download_state["component_sizes"]  # type: ignore[assignment]
    component_downloaded: dict[str, int] = download_state["component_downloaded"]  # type: ignore[assignment]

    component_downloaded[component_id] = max(downloaded, component_downloaded.get(component_id, 0))
    if total:
        component_sizes[component_id] = max(total, component_sizes.get(component_id, 0))

    total_estimated = max(
        int(download_state["total_estimated"]),
        sum(component_sizes.values()),
        sum(component_downloaded.values()),
    )
    download_state["total_estimated"] = total_estimated


def _mark_component_finished(download_state: dict[str, object], component_id: str) -> None:
    component_sizes: dict[str, int] = download_state["component_sizes"]  # type: ignore[assignment]
    component_downloaded: dict[str, int] = download_state["component_downloaded"]  # type: ignore[assignment]
    finished_components: set[str] = download_state["finished_components"]  # type: ignore[assignment]

    if component_id not in component_sizes:
        component_sizes[component_id] = component_downloaded.get(component_id, 0)
    elif component_downloaded.get(component_id):
        component_sizes[component_id] = max(component_sizes[component_id], component_downloaded[component_id])
    finished_components.add(component_id)
    download_state["total_estimated"] = max(
        int(download_state["total_estimated"]),
        sum(component_sizes.values()),
    )


def _get_completed_bytes(download_state: dict[str, object]) -> int:
    component_sizes: dict[str, int] = download_state["component_sizes"]  # type: ignore[assignment]
    component_downloaded: dict[str, int] = download_state["component_downloaded"]  # type: ignore[assignment]
    finished_components: set[str] = download_state["finished_components"]  # type: ignore[assignment]
    return sum(
        max(component_sizes.get(component_id, 0), component_downloaded.get(component_id, 0))
        for component_id in finished_components
    )


def _get_overall_downloaded(
    download_state: dict[str, object],
    current_component_id: str | None = None,
) -> int:
    completed_bytes = _get_completed_bytes(download_state)
    if not current_component_id:
        return completed_bytes

    component_downloaded: dict[str, int] = download_state["component_downloaded"]  # type: ignore[assignment]
    finished_components: set[str] = download_state["finished_components"]  # type: ignore[assignment]
    if current_component_id in finished_components:
        return completed_bytes
    return completed_bytes + component_downloaded.get(current_component_id, 0)


def _get_total_estimated(download_state: dict[str, object]) -> int:
    component_sizes: dict[str, int] = download_state["component_sizes"]  # type: ignore[assignment]
    total_estimated = max(int(download_state["total_estimated"]), sum(component_sizes.values()))
    download_state["total_estimated"] = total_estimated
    return total_estimated


def _get_progress_percent(
    download_state: dict[str, object],
    current_component_id: str | None = None,
) -> float:
    total_estimated = _get_total_estimated(download_state)
    if total_estimated <= 0:
        return 0.0

    calculated = min(
        100.0,
        _get_overall_downloaded(download_state, current_component_id=current_component_id)
        / total_estimated
        * 100.0,
    )
    last_progress = float(download_state.get("last_progress") or 0.0)
    final_progress = max(last_progress, calculated)
    download_state["last_progress"] = final_progress
    return final_progress


def _format_progress_bytes(downloaded: int, total: int) -> str:
    if total > 0:
        return f"{_format_bytes(downloaded)} / {_format_bytes(total)}"
    return _format_bytes(downloaded)


def _describe_stream_phase(info: dict) -> str:
    vcodec = str(info.get("vcodec") or "")
    acodec = str(info.get("acodec") or "")
    if vcodec != "none" and acodec == "none":
        return "正在下载视频流"
    if vcodec == "none" and acodec != "none":
        return "正在下载音频流"
    return "正在下载"


def _format_duration(seconds: object) -> str:
    if seconds in (None, ""):
        return "--"
    total_seconds = int(float(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_bytes(byte_count: int) -> str:
    if byte_count <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(byte_count)
    unit_index = min(int(math.log(value, 1024)), len(units) - 1)
    scaled = value / (1024**unit_index)
    return f"{scaled:.1f} {units[unit_index]}"


def _format_speed(speed: object) -> str:
    if not speed:
        return ""
    return f"{_format_bytes(int(float(speed)))}/s"


def _format_eta(eta: object) -> str:
    if eta in (None, ""):
        return ""
    seconds = int(float(eta))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"ETA {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"ETA {minutes:02d}:{secs:02d}"


def _friendly_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    message = message.removeprefix("ERROR: ").strip()
    return message


def _to_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
