from __future__ import annotations

import math
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from bili_gui_downloader.config import APP_NAME, AppConfig, AppPaths, save_config
from bili_gui_downloader.models import DownloadSummary, VideoMetadata
from bili_gui_downloader.services.browser_launcher import (
    build_cookies_from_browser,
    launch_login_browser,
)
from bili_gui_downloader.services.downloader import BiliDownloader, is_ffmpeg_available
from bili_gui_downloader.services.link_parser import extract_video_urls


class MainWindow:
    def __init__(self, config: AppConfig, paths: AppPaths) -> None:
        self.config = config
        self.paths = paths

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1140x860")
        self.root.minsize(1020, 740)

        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.video_items: list[VideoMetadata] = []
        self.result_row_by_task_id: dict[str, str] = {}
        self.progress_row_by_task_id: dict[str, str] = {}
        self.item_runtime: dict[str, dict[str, object]] = {}
        self.last_output_dir: Path | None = None

        self.use_default_dir_var = tk.BooleanVar(value=bool(self.config.default_download_dir))
        self.progress_text_var = tk.StringVar(value="未开始")
        self.detail_text_var = tk.StringVar(value="请先输入链接，然后点击“读取链接”。")
        self.default_path_text_var = tk.StringVar()

        self._build_widgets()
        self._refresh_default_path_hint()
        self.root.after(150, self._process_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._push_log("应用已启动。建议第一次先点“打开登录浏览器”登录你的 B 站账号。")
        self._push_log(f"浏览器资料目录：{self.paths.browser_profile_dir}")
        if not is_ffmpeg_available():
            self._push_log("未检测到 ffmpeg。部分高清视频可能无法正常合并音视频。")

    def run(self) -> None:
        self.root.mainloop()

    def _build_widgets(self) -> None:
        root_frame = ttk.Frame(self.root, padding=12)
        root_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root_frame)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text=APP_NAME, font=("Microsoft YaHei UI", 15, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="设置", command=self._open_settings_dialog).pack(side=tk.RIGHT)

        input_frame = ttk.LabelFrame(root_frame, text="输入 B 站链接（支持多行）", padding=10)
        input_frame.pack(fill=tk.X, pady=(0, 10))
        self.input_text = scrolledtext.ScrolledText(input_frame, height=6, wrap=tk.WORD)
        self.input_text.pack(fill=tk.X, expand=True)

        button_row = ttk.Frame(root_frame)
        button_row.pack(fill=tk.X, pady=(0, 10))

        self.read_button = ttk.Button(button_row, text="读取链接", command=self._start_read_links)
        self.read_button.pack(side=tk.LEFT)

        self.download_button = ttk.Button(
            button_row,
            text="开始下载",
            command=self._start_download,
            state=tk.DISABLED,
        )
        self.download_button.pack(side=tk.LEFT, padx=(8, 0))

        self.stop_button = ttk.Button(
            button_row,
            text="停止任务",
            command=self._stop_current_task,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        self.login_button = ttk.Button(button_row, text="打开登录浏览器", command=self._open_login_browser)
        self.login_button.pack(side=tk.LEFT, padx=(8, 0))

        self.open_folder_button = ttk.Button(
            button_row,
            text="打开下载目录",
            command=self._open_last_folder,
            state=tk.DISABLED,
        )
        self.open_folder_button.pack(side=tk.LEFT, padx=(8, 0))

        preview_frame = ttk.LabelFrame(root_frame, text="读取结果", padding=10)
        preview_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 10))

        result_columns = ("title", "uploader", "duration", "quality", "status")
        self.result_tree = ttk.Treeview(
            preview_frame,
            columns=result_columns,
            show="headings",
            height=8,
        )
        self.result_tree.heading("title", text="标题")
        self.result_tree.heading("uploader", text="作者")
        self.result_tree.heading("duration", text="时长")
        self.result_tree.heading("quality", text="预计规格")
        self.result_tree.heading("status", text="状态")
        self.result_tree.column("title", width=420, anchor=tk.W)
        self.result_tree.column("uploader", width=150, anchor=tk.W)
        self.result_tree.column("duration", width=90, anchor=tk.CENTER)
        self.result_tree.column("quality", width=240, anchor=tk.W)
        self.result_tree.column("status", width=160, anchor=tk.W)

        result_scroll = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=result_scroll.set)
        self.result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        result_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        path_frame = ttk.Frame(root_frame)
        path_frame.pack(fill=tk.X, pady=(0, 10))
        self.default_path_check = ttk.Checkbutton(
            path_frame,
            variable=self.use_default_dir_var,
            textvariable=self.default_path_text_var,
        )
        self.default_path_check.pack(side=tk.LEFT)

        progress_frame = ttk.LabelFrame(root_frame, text="下载进度", padding=10)
        progress_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 10))

        self.progress_bar = ttk.Progressbar(progress_frame, orient=tk.HORIZONTAL, mode="determinate", maximum=1.0)
        self.progress_bar.pack(fill=tk.X)
        ttk.Label(progress_frame, textvariable=self.progress_text_var).pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(progress_frame, textvariable=self.detail_text_var).pack(anchor=tk.W, pady=(2, 8))

        progress_columns = ("title", "status", "progress", "downloaded", "speed", "eta")
        self.progress_tree = ttk.Treeview(
            progress_frame,
            columns=progress_columns,
            show="headings",
            height=6,
        )
        self.progress_tree.heading("title", text="视频")
        self.progress_tree.heading("status", text="当前状态")
        self.progress_tree.heading("progress", text="进度")
        self.progress_tree.heading("downloaded", text="已下载 / 总大小")
        self.progress_tree.heading("speed", text="当前速度")
        self.progress_tree.heading("eta", text="ETA")
        self.progress_tree.column("title", width=320, anchor=tk.W)
        self.progress_tree.column("status", width=220, anchor=tk.W)
        self.progress_tree.column("progress", width=90, anchor=tk.CENTER)
        self.progress_tree.column("downloaded", width=180, anchor=tk.W)
        self.progress_tree.column("speed", width=110, anchor=tk.CENTER)
        self.progress_tree.column("eta", width=90, anchor=tk.CENTER)

        progress_scroll = ttk.Scrollbar(progress_frame, orient=tk.VERTICAL, command=self.progress_tree.yview)
        self.progress_tree.configure(yscrollcommand=progress_scroll.set)
        self.progress_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        progress_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        log_frame = ttk.LabelFrame(root_frame, text="日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _open_settings_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.geometry("520x260")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        container = ttk.Frame(dialog, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        default_dir_var = tk.StringVar(value=self.config.default_download_dir)
        browser_var = tk.StringVar(value=self.config.browser_preference)
        parallel_var = tk.StringVar(value=str(self.config.concurrent_downloads))
        fragment_var = tk.StringVar(value=str(self.config.concurrent_fragments))

        ttk.Label(container, text="默认下载目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(container, textvariable=default_dir_var, width=46).grid(row=1, column=0, sticky="ew", pady=(4, 12))
        ttk.Button(
            container,
            text="选择目录",
            command=lambda: self._choose_path_into_var(default_dir_var),
        ).grid(row=1, column=1, padx=(8, 0), pady=(4, 12))

        ttk.Label(container, text="登录浏览器").grid(row=2, column=0, sticky="w")
        ttk.Combobox(
            container,
            textvariable=browser_var,
            state="readonly",
            values=["auto", "chrome", "edge"],
        ).grid(row=3, column=0, sticky="w", pady=(4, 12))

        ttk.Label(container, text="同时下载数量").grid(row=4, column=0, sticky="w")
        ttk.Spinbox(container, from_=1, to=4, textvariable=parallel_var, width=8).grid(row=5, column=0, sticky="w", pady=(4, 12))

        ttk.Label(container, text="单任务加速线程").grid(row=4, column=1, sticky="w")
        ttk.Spinbox(container, from_=1, to=8, textvariable=fragment_var, width=8).grid(row=5, column=1, sticky="w", pady=(4, 12))

        button_frame = ttk.Frame(container)
        button_frame.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(
            button_frame,
            text="保存",
            command=lambda: self._save_settings(
                dialog,
                default_dir_var.get(),
                browser_var.get(),
                parallel_var.get(),
                fragment_var.get(),
            ),
        ).pack(side=tk.RIGHT, padx=(0, 8))

        container.columnconfigure(0, weight=1)

    def _choose_path_into_var(self, path_var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="选择默认下载目录",
            initialdir=path_var.get().strip() or str(Path.home()),
        )
        if chosen:
            path_var.set(chosen)

    def _save_settings(
        self,
        dialog: tk.Toplevel,
        default_dir: str,
        browser_preference: str,
        concurrent_downloads: str,
        concurrent_fragments: str,
    ) -> None:
        default_dir = default_dir.strip()
        if default_dir:
            default_path = Path(default_dir).resolve()
            default_path.mkdir(parents=True, exist_ok=True)
            default_dir = str(default_path)

        self.config.default_download_dir = default_dir
        self.config.browser_preference = browser_preference or "auto"
        self.config.concurrent_downloads = _safe_int(concurrent_downloads, 2)
        self.config.concurrent_fragments = _safe_int(concurrent_fragments, 4)
        save_config(self.paths, self.config)

        self.use_default_dir_var.set(bool(self.config.default_download_dir))
        self._refresh_default_path_hint()
        self._push_log("设置已保存。")
        dialog.destroy()

    def _start_read_links(self) -> None:
        if self._is_busy():
            messagebox.showwarning("任务进行中", "当前还有任务在执行，请先等待或停止。")
            return

        raw_text = self.input_text.get("1.0", tk.END).strip()
        urls = extract_video_urls(raw_text)
        if not urls:
            messagebox.showerror("没有识别到链接", "请输入至少一个有效的 B 站视频链接或 BV 号。")
            return

        self._reset_video_tables()
        self.progress_bar.configure(maximum=max(len(urls), 1), value=0)
        self.progress_text_var.set("正在读取链接")
        self.detail_text_var.set(f"共识别到 {len(urls)} 个链接，正在读取标题和规格。")
        self._set_busy_state(True)
        self._push_log(f"开始读取 {len(urls)} 个链接。")

        self.worker_thread = threading.Thread(
            target=self._read_links_worker,
            args=(urls,),
            daemon=True,
        )
        self.worker_thread.start()

    def _read_links_worker(self, urls: list[str]) -> None:
        try:
            downloader = BiliDownloader(self.config, self.paths, log_callback=self._push_log)
            items = downloader.fetch_metadata_batch(urls)
            self.ui_queue.put(("metadata_ready", items))
        except Exception as exc:
            self.ui_queue.put(("error", f"读取链接失败：{exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    def _start_download(self) -> None:
        if self._is_busy():
            messagebox.showwarning("任务进行中", "当前还有任务在执行，请先等待或停止。")
            return

        if not self.video_items:
            messagebox.showwarning("还没读取链接", "请先点击“读取链接”，确认标题后再下载。")
            return

        output_dir = self._resolve_output_dir()
        if output_dir is None:
            return

        valid_items = [item for item in self.video_items if not item.error_message]
        if not valid_items:
            messagebox.showerror("没有可下载项", "当前列表里没有成功读取到的视频。")
            return

        self.last_output_dir = output_dir
        self.open_folder_button.configure(state=tk.NORMAL)
        self.stop_event.clear()

        for item in valid_items:
            self.item_runtime[item.task_id] = {
                "status": "准备下载",
                "detail": "正在排队",
                "progress": 0.0,
                "downloaded_text": _initial_downloaded_text(item),
                "speed_text": "",
                "speed_bps": 0.0,
                "eta_text": "",
            }
            self._render_progress_row(item.task_id)

        self._refresh_overall_progress()
        self._set_busy_state(True)
        self._push_log(f"开始下载，保存目录：{output_dir}")

        self.worker_thread = threading.Thread(
            target=self._download_worker,
            args=(output_dir,),
            daemon=True,
        )
        self.worker_thread.start()

    def _download_worker(self, output_dir: Path) -> None:
        try:
            downloader = BiliDownloader(self.config, self.paths, log_callback=self._push_log)
            summary = downloader.download_batch(
                self.video_items,
                output_dir=output_dir,
                stop_event=self.stop_event,
                item_callback=self._queue_item_update,
            )
            self.ui_queue.put(("download_done", summary))
        except Exception as exc:
            self.ui_queue.put(("error", f"下载失败：{exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    def _resolve_output_dir(self) -> Path | None:
        if self.use_default_dir_var.get() and self.config.default_download_dir:
            output_dir = Path(self.config.default_download_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir

        chosen = filedialog.askdirectory(
            parent=self.root,
            title="选择本次下载目录",
            initialdir=self.config.default_download_dir or str(Path.home()),
        )
        if not chosen:
            return None

        output_dir = Path(chosen).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self.config.default_download_dir = str(output_dir)
        self.use_default_dir_var.set(True)
        save_config(self.paths, self.config)
        self._refresh_default_path_hint()
        self._push_log(f"已将首次选择的目录保存为默认下载目录：{output_dir}")
        return output_dir

    def _open_login_browser(self) -> None:
        try:
            browser = launch_login_browser(self.paths.browser_profile_dir, self.config.browser_preference)
            self._push_log(
                f"已打开登录浏览器：{browser.display_name}。请在新窗口登录 B 站，登录完成后直接关闭那个窗口。"
            )
        except Exception as exc:
            messagebox.showerror("无法打开登录浏览器", str(exc))

    def _stop_current_task(self) -> None:
        self.stop_event.set()
        self._push_log("已请求停止任务，正在等待当前下载线程退出。")

    def _open_last_folder(self) -> None:
        if self.last_output_dir and self.last_output_dir.exists():
            os.startfile(self.last_output_dir)

    def _queue_item_update(self, task_id: str, payload: dict) -> None:
        self.ui_queue.put(("item_update", (task_id, payload)))

    def _process_ui_queue(self) -> None:
        try:
            while True:
                event, payload = self.ui_queue.get_nowait()
                if event == "log":
                    self._append_log(str(payload))
                elif event == "error":
                    self._append_log(str(payload))
                    messagebox.showerror("执行失败", str(payload))
                elif event == "idle":
                    self._set_busy_state(False)
                elif event == "metadata_ready":
                    self._render_metadata(list(payload))
                elif event == "item_update":
                    task_id, data = payload
                    self._apply_item_update(task_id, data)
                elif event == "download_done":
                    self._handle_download_done(payload)
        except queue.Empty:
            pass
        self.root.after(150, self._process_ui_queue)

    def _render_metadata(self, items: list[VideoMetadata]) -> None:
        self.video_items = items
        self.result_row_by_task_id.clear()
        self.progress_row_by_task_id.clear()
        self.item_runtime.clear()

        success_count = 0
        for item in items:
            status_text = item.error_message or item.status
            result_row_id = self.result_tree.insert(
                "",
                tk.END,
                values=(
                    item.title,
                    item.uploader,
                    item.duration_text,
                    item.best_quality_text,
                    status_text,
                ),
            )
            self.result_row_by_task_id[item.task_id] = result_row_id

            initial_progress = "0.0%" if not item.error_message else "--"
            initial_downloaded = _initial_downloaded_text(item) if not item.error_message else "--"
            progress_row_id = self.progress_tree.insert(
                "",
                tk.END,
                values=(
                    item.title,
                    status_text,
                    initial_progress,
                    initial_downloaded,
                    "--",
                    "--",
                ),
            )
            self.progress_row_by_task_id[item.task_id] = progress_row_id

            if not item.error_message:
                success_count += 1
                self.item_runtime[item.task_id] = {
                    "status": "待下载",
                    "detail": "",
                    "progress": 0.0,
                    "downloaded_text": initial_downloaded,
                    "speed_text": "",
                    "speed_bps": 0.0,
                    "eta_text": "",
                }

        self.download_button.configure(state=tk.NORMAL if success_count else tk.DISABLED)
        self.progress_bar.configure(maximum=max(success_count, 1), value=0)
        self.progress_text_var.set("读取完成")
        self.detail_text_var.set(f"共读取 {len(items)} 条，成功 {success_count} 条。")
        self._push_log(f"读取完成，共 {len(items)} 条，成功 {success_count} 条。")

        if build_cookies_from_browser(self.paths.browser_profile_dir, self.config.browser_preference):
            self._push_log("检测到独立浏览器资料目录，下次读取和下载会自动尝试带上登录态。")
        else:
            self._push_log("当前还没有检测到登录态。如果需要更高规格，请先点击“打开登录浏览器”。")

    def _apply_item_update(self, task_id: str, payload: dict) -> None:
        runtime = self.item_runtime.setdefault(
            task_id,
            {
                "status": "待下载",
                "detail": "",
                "progress": 0.0,
                "downloaded_text": "--",
                "speed_text": "",
                "speed_bps": 0.0,
                "eta_text": "",
            },
        )

        status = str(payload.get("status") or runtime["status"])
        detail = str(payload.get("detail") or "")
        progress = float(payload.get("progress") or runtime["progress"])
        if status in {"已完成", "失败", "已停止"}:
            progress = 100.0

        speed_bps = float(payload.get("speed_bps") or 0.0)
        speed_text = str(payload.get("speed_text") or "")
        eta_text = str(payload.get("eta_text") or "")
        downloaded_text = str(payload.get("downloaded_text") or runtime["downloaded_text"])

        if status != "下载中":
            speed_bps = 0.0
            speed_text = ""
            if status != "准备下载":
                eta_text = ""

        runtime.update(
            {
                "status": status,
                "detail": detail,
                "progress": progress,
                "downloaded_text": downloaded_text,
                "speed_text": speed_text,
                "speed_bps": speed_bps,
                "eta_text": eta_text,
            }
        )

        result_row_id = self.result_row_by_task_id.get(task_id)
        if result_row_id:
            current_values = list(self.result_tree.item(result_row_id, "values"))
            if current_values:
                current_values[4] = status if not detail else f"{status}·{detail}"
                self.result_tree.item(result_row_id, values=current_values)

        self._render_progress_row(task_id)
        self._refresh_overall_progress()

    def _render_progress_row(self, task_id: str) -> None:
        row_id = self.progress_row_by_task_id.get(task_id)
        item = self._find_video_item(task_id)
        runtime = self.item_runtime.get(task_id)
        if not row_id or item is None or runtime is None:
            return

        status = str(runtime["status"])
        detail = str(runtime["detail"])
        progress = float(runtime["progress"])
        downloaded_text = str(runtime["downloaded_text"])
        speed_text = str(runtime["speed_text"])
        eta_text = str(runtime["eta_text"])

        display_status = status if not detail else f"{status} | {detail}"
        self.progress_tree.item(
            row_id,
            values=(
                item.title,
                display_status,
                f"{progress:.1f}%",
                downloaded_text or "--",
                speed_text or "--",
                eta_text or "--",
            ),
        )

    def _handle_download_done(self, summary: DownloadSummary) -> None:
        self._refresh_overall_progress(force_finished=True)
        self.progress_text_var.set("下载结束")
        self.detail_text_var.set(
            f"成功 {summary.success_count}，失败 {summary.failed_count}，停止 {summary.stopped_count}。"
        )
        self._push_log(
            f"下载结束。成功 {summary.success_count}，失败 {summary.failed_count}，停止 {summary.stopped_count}。"
        )

    def _refresh_default_path_hint(self) -> None:
        if self.config.default_download_dir:
            self.default_path_text_var.set(f"保存到默认下载路径：{self.config.default_download_dir}")
            self.default_path_check.configure(state=tk.NORMAL)
            return

        self.default_path_text_var.set("保存到默认下载路径：尚未设置（首次下载会要求你选择目录）")
        self.default_path_check.configure(state=tk.DISABLED)

    def _refresh_overall_progress(self, force_finished: bool = False) -> None:
        valid_items = [item for item in self.video_items if not item.error_message]
        total_items = len(valid_items)
        if total_items == 0:
            self.progress_bar.configure(maximum=1.0, value=0.0)
            return

        progress_units = 0.0
        completed_count = 0
        active_count = 0
        merging_count = 0
        total_speed_bps = 0.0

        for item in valid_items:
            runtime = self.item_runtime.get(item.task_id, {})
            status = str(runtime.get("status") or "待下载")
            progress = float(runtime.get("progress") or 0.0)
            speed_bps = float(runtime.get("speed_bps") or 0.0)

            if force_finished and status in {"已完成", "失败", "已停止"}:
                progress = 100.0

            progress_units += min(progress, 100.0) / 100.0
            if progress >= 100.0 or status in {"已完成", "失败", "已停止"}:
                completed_count += 1
            elif status == "下载中":
                active_count += 1
            elif status == "合并音视频":
                merging_count += 1

            total_speed_bps += max(speed_bps, 0.0)

        self.progress_bar.configure(maximum=float(total_items))
        self.progress_bar["value"] = progress_units
        self.progress_text_var.set(f"下载中，已完成 {completed_count}/{total_items}")

        detail_parts = [
            f"总速度 {_format_speed(total_speed_bps)}",
            f"活跃下载 {active_count} 个",
        ]
        if merging_count:
            detail_parts.append(f"合并中 {merging_count} 个")
        waiting_count = max(total_items - completed_count - active_count - merging_count, 0)
        if waiting_count:
            detail_parts.append(f"等待中 {waiting_count} 个")
        self.detail_text_var.set(" | ".join(detail_parts))

    def _set_busy_state(self, busy: bool) -> None:
        self.read_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.download_button.configure(
            state=tk.DISABLED if busy else (tk.NORMAL if self._has_downloadable_items() else tk.DISABLED)
        )
        self.stop_button.configure(state=tk.NORMAL if busy else tk.DISABLED)
        self.login_button.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def _reset_video_tables(self) -> None:
        self.video_items = []
        self.result_row_by_task_id.clear()
        self.progress_row_by_task_id.clear()
        self.item_runtime.clear()
        for row_id in self.result_tree.get_children():
            self.result_tree.delete(row_id)
        for row_id in self.progress_tree.get_children():
            self.progress_tree.delete(row_id)

    def _find_video_item(self, task_id: str) -> VideoMetadata | None:
        for item in self.video_items:
            if item.task_id == task_id:
                return item
        return None

    def _is_busy(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def _has_downloadable_items(self) -> bool:
        return any(not item.error_message for item in self.video_items)

    def _on_close(self) -> None:
        self.stop_event.set()
        self.root.destroy()

    def _push_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)


def _initial_downloaded_text(item: VideoMetadata) -> str:
    if item.estimated_total_bytes > 0:
        return f"0 B / {_format_bytes(item.estimated_total_bytes)}"
    return "0 B"


def _safe_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _format_bytes(byte_count: int) -> str:
    if byte_count <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(byte_count)
    unit_index = min(int(math.log(value, 1024)), len(units) - 1)
    scaled = value / (1024**unit_index)
    return f"{scaled:.1f} {units[unit_index]}"


def _format_speed(speed_bps: float) -> str:
    if speed_bps <= 0:
        return "0 B/s"
    return f"{_format_bytes(int(speed_bps))}/s"
