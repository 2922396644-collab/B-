from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VideoMetadata:
    task_id: str
    input_index: int
    source_url: str
    normalized_url: str
    title: str = ""
    uploader: str = ""
    duration_text: str = "--"
    best_quality_text: str = "--"
    video_id: str = ""
    status: str = "待读取"
    error_message: str = ""
    output_dir: str = ""
    estimated_total_bytes: int = 0
    component_estimated_bytes: dict[str, int] = field(default_factory=dict)


@dataclass
class DownloadSummary:
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    stopped_count: int = 0
    final_output_dir: str = ""
