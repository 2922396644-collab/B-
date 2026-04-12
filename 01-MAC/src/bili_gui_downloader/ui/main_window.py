from __future__ import annotations

import math
import queue
import subprocess
import sys
import threading
import traceback
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from bili_gui_downloader.config import APP_NAME, AppConfig, AppPaths, save_config
from bili_gui_downloader.models import DownloadSummary, VideoMetadata
from bili_gui_downloader.services.browser_launcher import build_cookies_from_browser, launch_login_browser
from bili_gui_downloader.services.downloader import BiliDownloader, is_ffmpeg_available
from bili_gui_downloader.services.history_store import (
    DownloadHistoryEntry,
    append_history_entries,
    clear_history,
    load_history,
    resolve_existing_path,
)
from bili_gui_downloader.services.link_parser import extract_video_urls


class MainWindow(QMainWindow):
    MAX_UI_EVENTS_PER_TICK = 120

    def __init__(self, config: AppConfig, paths: AppPaths) -> None:
        super().__init__()
        self.config = config
        self.paths = paths

        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.pending_item_updates: dict[str, dict[str, object]] = {}
        self.pending_item_updates_lock = threading.Lock()
        self.log_file_lock = threading.Lock()
        self.session_log_path = self.paths.logs_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.video_items: list[VideoMetadata] = []
        self.result_row_by_task_id: dict[str, int] = {}
        self.progress_row_by_task_id: dict[str, int] = {}
        self.item_runtime: dict[str, dict[str, object]] = {}
        self.last_output_dir: Path | None = None
        self.use_default_dir = bool(self.config.default_download_dir)
        self.effective_theme_mode = _resolve_effective_theme_mode(self.config.theme_mode)
        self.last_system_theme_mode = _detect_system_theme_mode()

        self.setWindowTitle(APP_NAME)
        self.resize(1380, 920)
        self.setMinimumSize(1180, 780)
        self._set_window_icon()
        self._apply_base_font()
        self._build_ui()
        self._apply_styles()
        self._refresh_default_path_hint()
        self._refresh_login_status()
        self._refresh_system_status()

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._process_ui_queue)
        self.queue_timer.start(120)

        self.theme_timer = QTimer(self)
        self.theme_timer.timeout.connect(self._poll_system_theme)
        self.theme_timer.start(2500)

        self._push_log(f"本次会话日志：{self.session_log_path}")
        self._push_log("应用已启动。建议第一次先点“打开登录浏览器”登录你的 B 站账号。")
        self._push_log(f"浏览器资料目录：{self.paths.browser_profile_dir}")
        if not is_ffmpeg_available():
            self._push_log("未检测到 ffmpeg，部分高清视频可能无法正常合并音视频。")

    def run(self) -> None:
        self.show()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = self._build_sidebar()
        root_layout.addWidget(self.sidebar)

        content_shell = QWidget()
        content_shell.setObjectName("ContentShell")
        content_layout = QVBoxLayout(content_shell)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        content_layout.addWidget(self._build_top_bar())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setObjectName("BodyScroll")

        scroll_widget = QWidget()
        body_layout = QVBoxLayout(scroll_widget)
        body_layout.setContentsMargins(30, 28, 30, 28)
        body_layout.setSpacing(20)

        body_layout.addWidget(self._build_hero_section())
        body_layout.addWidget(self._build_input_card())
        body_layout.addLayout(self._build_action_row())
        body_layout.addWidget(self._build_results_card())
        body_layout.addWidget(self._build_progress_card())
        body_layout.addWidget(self._build_footer_bar())
        body_layout.addWidget(self._build_log_card(), stretch=1)
        body_layout.addStretch(1)

        scroll.setWidget(scroll_widget)
        content_layout.addWidget(scroll)
        root_layout.addWidget(content_shell, 1)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(248)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 22, 18, 22)
        layout.setSpacing(18)

        brand_title = QLabel("高码流引擎")
        brand_title.setObjectName("SidebarTitle")
        brand_subtitle = QLabel("macOS 版")
        brand_subtitle.setObjectName("SidebarSubtitle")

        brand_box = QVBoxLayout()
        brand_box.setSpacing(4)
        brand_box.addWidget(brand_title)
        brand_box.addWidget(brand_subtitle)
        layout.addLayout(brand_box)

        self.queue_nav_button = self._make_nav_button("下载队列", checked=True)
        self.completed_nav_button = self._make_nav_button("已完成")
        self.analytics_nav_button = self._make_nav_button("数据统计")
        self.settings_nav_button = self._make_nav_button("设置")

        self.completed_nav_button.clicked.connect(self._open_history_dialog)
        self.analytics_nav_button.clicked.connect(self._open_analytics_dialog)
        self.settings_nav_button.clicked.connect(self._open_settings_dialog)

        nav_box = QVBoxLayout()
        nav_box.setSpacing(8)
        nav_box.addWidget(self.queue_nav_button)
        nav_box.addWidget(self.completed_nav_button)
        nav_box.addWidget(self.analytics_nav_button)
        nav_box.addWidget(self.settings_nav_button)
        layout.addLayout(nav_box)

        login_card = QFrame()
        login_card.setObjectName("SidebarCard")
        login_layout = QVBoxLayout(login_card)
        login_layout.setContentsMargins(14, 14, 14, 14)
        login_layout.setSpacing(10)

        login_title = QLabel("B站登录状态")
        login_title.setObjectName("CardTitle")
        self.login_status_label = QLabel("未检测到登录资料")
        self.login_status_label.setObjectName("SidebarMeta")
        self.sidebar_login_button = QPushButton("打开登录浏览器")
        self.sidebar_login_button.setObjectName("SidebarActionButton")
        self.sidebar_login_button.clicked.connect(self._open_login_browser)

        login_layout.addWidget(login_title)
        login_layout.addWidget(self.login_status_label)
        login_layout.addWidget(self.sidebar_login_button)
        layout.addWidget(login_card)

        layout.addStretch(1)

        self.log_nav_button = self._make_footer_link_button("日志")
        self.system_nav_button = self._make_footer_link_button("系统状态")
        self.log_nav_button.clicked.connect(lambda: self.log_edit.setFocus())
        self.system_nav_button.clicked.connect(
            lambda: self._show_placeholder_message(self.system_status_chip.text())
        )

        footer_box = QVBoxLayout()
        footer_box.setSpacing(8)
        footer_box.addWidget(self.log_nav_button)
        footer_box.addWidget(self.system_nav_button)
        layout.addLayout(footer_box)
        return sidebar

    def _build_top_bar(self) -> QWidget:
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(68)

        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(14)

        left_group = QHBoxLayout()
        left_group.setSpacing(22)

        app_label = QLabel(APP_NAME)
        app_label.setObjectName("TopBrand")
        left_group.addWidget(app_label)

        self.top_downloads_button = self._make_top_tab_button("下载任务", checked=True)
        self.top_history_button = self._make_top_tab_button("下载历史")
        self.top_settings_button = self._make_top_tab_button("设置")
        self.top_history_button.clicked.connect(self._open_history_dialog)
        self.top_settings_button.clicked.connect(self._open_settings_dialog)

        left_group.addWidget(self.top_downloads_button)
        left_group.addWidget(self.top_history_button)
        left_group.addWidget(self.top_settings_button)
        left_group.addStretch(1)
        layout.addLayout(left_group, 1)

        self.theme_toggle_button = self._make_round_button("")
        self.theme_toggle_button.clicked.connect(self._toggle_theme_mode)
        layout.addWidget(self.theme_toggle_button)

        self.help_button = self._make_round_button("帮助")
        self.help_button.clicked.connect(
            lambda: self._show_placeholder_message("当前版本已对齐下载主流程，并补齐了历史记录、统计和主题设置。")
        )
        layout.addWidget(self.help_button)
        return top_bar

    def _build_hero_section(self) -> QWidget:
        hero = QWidget()
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(6)

        title = QLabel("《B站高码流视频下载》")
        title.setObjectName("HeroTitle")
        subtitle = QLabel("面向桌面端的高码流视频下载工具，适合批量收藏、归档和高质量保存。")
        subtitle.setObjectName("HeroSubtitle")
        subtitle.setWordWrap(True)

        left.addWidget(title)
        left.addWidget(subtitle)
        layout.addLayout(left, 1)

        self.system_status_chip = QLabel("系统检测中")
        self.system_status_chip.setObjectName("SystemChip")
        self.system_status_chip.setAlignment(Qt.AlignCenter)
        self.system_status_chip.setFixedHeight(34)
        layout.addWidget(self.system_status_chip, 0, Qt.AlignTop)
        return hero

    def _build_input_card(self) -> QWidget:
        card = self._make_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title = QLabel("链接输入")
        title.setObjectName("CardTitle")
        helper = QLabel("支持多行")
        helper.setObjectName("MutedTinyLabel")
        helper.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(helper)
        layout.addLayout(header)

        self.input_edit = QTextEdit()
        self.input_edit.setObjectName("InputEditor")
        self.input_edit.setPlaceholderText("请粘贴 B 站视频链接，支持一行一个，也支持批量粘贴。")
        self.input_edit.setMinimumHeight(180)
        layout.addWidget(self.input_edit)
        return card

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.read_button = QPushButton("读取链接")
        self.read_button.setObjectName("PrimaryButton")
        self.read_button.clicked.connect(self._start_read_links)

        self.download_button = QPushButton("开始下载")
        self.download_button.setObjectName("SecondaryButton")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._start_download)

        self.stop_button = QPushButton("停止任务")
        self.stop_button.setObjectName("GhostButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_current_task)

        self.login_button = QPushButton("打开登录浏览器")
        self.login_button.setObjectName("LinkButton")
        self.login_button.clicked.connect(self._open_login_browser)

        self.open_folder_button = QPushButton("打开下载目录")
        self.open_folder_button.setObjectName("GhostButton")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_last_folder)

        row.addWidget(self.read_button)
        row.addWidget(self.download_button)
        row.addWidget(self.stop_button)
        row.addSpacing(8)
        row.addWidget(self.login_button)
        row.addWidget(self.open_folder_button)
        row.addStretch(1)
        return row

    def _build_results_card(self) -> QWidget:
        card = self._make_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QLabel("读取结果")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        self.result_stack = QStackedWidget()
        self.result_stack.addWidget(
            self._build_empty_placeholder(
                "请先粘贴链接并点击“读取链接”",
                "读取后会在这里显示标题、作者、时长和预计规格。",
            )
        )

        self.result_table = self._make_table(
            ["标题", "作者", "时长", "预计规格", "状态"],
            [420, 140, 90, 230, 160],
        )
        self.result_stack.addWidget(self.result_table)
        layout.addWidget(self.result_stack)
        return card

    def _build_progress_card(self) -> QWidget:
        card = self._make_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QLabel("下载进度")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        self.summary_progress = QProgressBar()
        self.summary_progress.setObjectName("SummaryProgress")
        self.summary_progress.setRange(0, 1000)
        self.summary_progress.setValue(0)
        self.summary_progress.setTextVisible(False)
        layout.addWidget(self.summary_progress)

        self.summary_title_label = QLabel("未开始")
        self.summary_title_label.setObjectName("SummaryTitle")
        self.summary_detail_label = QLabel("总速度 0 B/s | 活跃下载 0 个")
        self.summary_detail_label.setObjectName("SummaryDetail")
        layout.addWidget(self.summary_title_label)
        layout.addWidget(self.summary_detail_label)

        self.progress_stack = QStackedWidget()
        self.progress_stack.addWidget(
            self._build_empty_placeholder(
                "还没有下载任务",
                "读取视频信息后，开始下载时会在这里逐条显示每个视频的进度。",
            )
        )

        self.progress_table = self._make_table(
            ["视频", "当前状态", "进度", "已下载 / 总大小", "速度", "ETA"],
            [320, 220, 90, 190, 110, 90],
        )
        self.progress_stack.addWidget(self.progress_table)
        layout.addWidget(self.progress_stack)
        return card

    def _build_footer_bar(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("FooterBar")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(14)

        label = QLabel("默认路径：")
        label.setObjectName("FooterLabel")

        self.default_path_value_label = QLabel("尚未设置")
        self.default_path_value_label.setObjectName("PathCode")
        self.default_path_value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.edit_default_path_button = QPushButton("修改")
        self.edit_default_path_button.setObjectName("InlineLinkButton")
        self.edit_default_path_button.clicked.connect(self._open_settings_dialog)

        left = QHBoxLayout()
        left.setSpacing(10)
        left.addWidget(label)
        left.addWidget(self.default_path_value_label, 1)
        left.addWidget(self.edit_default_path_button)
        layout.addLayout(left, 1)

        self.default_path_checkbox = QCheckBox("始终使用默认路径")
        self.default_path_checkbox.setChecked(self.use_default_dir)
        self.default_path_checkbox.toggled.connect(self._toggle_use_default_dir)
        layout.addWidget(self.default_path_checkbox, 0, Qt.AlignRight)
        return footer

    def _build_log_card(self) -> QWidget:
        card = self._make_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QLabel("日志")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setObjectName("LogEdit")
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(2000)
        self.log_edit.setMinimumHeight(160)
        layout.addWidget(self.log_edit)
        return card

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        return card

    def _build_empty_placeholder(self, title: str, subtitle: str) -> QWidget:
        widget = QWidget()
        widget.setObjectName("EmptyState")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignCenter)

        icon_label = QLabel("↓")
        icon_label.setObjectName("EmptyIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_font = QFont(self.font())
        icon_font.setPointSize(28)
        icon_font.setBold(True)
        icon_label.setFont(icon_font)
        icon_label.setFixedSize(72, 72)

        title_label = QLabel(title)
        title_label.setObjectName("EmptyTitle")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setWordWrap(True)
        title_label.setMaximumWidth(560)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("EmptySubtitle")
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setWordWrap(True)
        subtitle_label.setFixedWidth(520)
        subtitle_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

        layout.addWidget(icon_label)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return widget

    def _make_table(self, headers: list[str], widths: list[int]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setObjectName("DataTable")
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setMinimumHeight(220)
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setStretchLastSection(True)
        for index, width in enumerate(widths):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
            table.setColumnWidth(index, width)
        return table

    def _make_nav_button(self, text: str, checked: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setObjectName("SidebarNavSelected" if checked else "SidebarNavButton")
        if checked:
            button.clicked.connect(lambda: None)
        return button

    def _make_top_tab_button(self, text: str, checked: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setFlat(True)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setObjectName("TopTabSelected" if checked else "TopTabButton")
        if checked:
            button.clicked.connect(lambda: None)
        return button

    def _make_round_button(self, text: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setObjectName("RoundButton")
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        return button

    def _make_footer_link_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("SidebarFooterButton")
        return button

    def _apply_base_font(self) -> None:
        font = QFont(_preferred_ui_font_family(), 10)
        font.setStyleStrategy(QFont.PreferAntialias)
        self.setFont(font)

    def _apply_styles(self) -> None:
        self.effective_theme_mode = _resolve_effective_theme_mode(self.config.theme_mode)
        if self.config.theme_mode == "system":
            self.last_system_theme_mode = self.effective_theme_mode

        colors = _theme_colors(self.effective_theme_mode)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(_build_palette(colors))
            app.setStyleSheet(_build_stylesheet(colors))

        target_label = f"主题：{_theme_label(self.config.theme_mode)}"
        if hasattr(self, "theme_toggle_button"):
            self.theme_toggle_button.setText(target_label)
            self.theme_toggle_button.setToolTip(
                f"当前设置为{_theme_label(self.config.theme_mode)}，实际显示为{_theme_label(self.effective_theme_mode)}"
            )
        return

        self.setStyleSheet(
            """
            QWidget#Root {
                background: #f3efee;
                color: #1f2426;
            }
            QFrame#Sidebar {
                background: #fbf8f7;
                border-right: 1px solid #e7dfde;
            }
            QLabel#SidebarTitle {
                font-size: 22px;
                font-weight: 800;
                color: #1f2426;
            }
            QLabel#SidebarSubtitle {
                font-size: 11px;
                letter-spacing: 1px;
                color: #7b898a;
            }
            QPushButton#SidebarNavButton, QPushButton#SidebarNavSelected {
                border: none;
                border-radius: 10px;
                text-align: left;
                padding: 12px 14px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton#SidebarNavButton {
                background: transparent;
                color: #445354;
            }
            QPushButton#SidebarNavButton:hover {
                background: #efebea;
            }
            QPushButton#SidebarNavSelected {
                background: #0c7b7b;
                color: white;
            }
            QFrame#SidebarCard {
                background: #ffffff;
                border: 1px solid #e7dfde;
                border-radius: 14px;
            }
            QLabel#CardTitle {
                font-size: 15px;
                font-weight: 800;
                color: #1f2426;
            }
            QLabel#SidebarMeta {
                color: #637274;
                font-size: 12px;
            }
            QPushButton#SidebarActionButton {
                border: none;
                border-radius: 10px;
                background: #dff4f4;
                color: #0c6d6d;
                padding: 10px 12px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#SidebarActionButton:hover {
                background: #cfeeee;
            }
            QPushButton#SidebarFooterButton {
                border: none;
                border-radius: 10px;
                background: transparent;
                color: #516063;
                padding: 10px 12px;
                text-align: left;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#SidebarFooterButton:hover {
                background: #efebea;
            }
            QFrame#TopBar {
                background: #f7f2f1;
                border-bottom: 1px solid #ece4e3;
            }
            QLabel#TopBrand {
                font-size: 16px;
                font-weight: 800;
                color: #1f2426;
            }
            QPushButton#TopTabButton, QPushButton#TopTabSelected {
                border: none;
                background: transparent;
                padding: 8px 0;
                font-size: 13px;
                font-weight: 700;
                color: #637274;
            }
            QPushButton#TopTabButton:hover {
                color: #223133;
            }
            QPushButton#TopTabSelected {
                color: #0c7b7b;
                border-bottom: 2px solid #0c7b7b;
            }
            QToolButton#RoundButton {
                border: 1px solid #e6dfdd;
                background: white;
                border-radius: 18px;
                padding: 7px 12px;
                font-size: 12px;
                font-weight: 700;
                color: #4a595b;
            }
            QToolButton#RoundButton:hover {
                background: #f6f1f0;
            }
            QLabel#HeroTitle {
                font-size: 28px;
                font-weight: 900;
                color: #171c1d;
            }
            QLabel#HeroSubtitle {
                font-size: 13px;
                color: #667577;
            }
            QLabel#SystemChip {
                border-radius: 17px;
                background: #f6fbfb;
                border: 1px solid #dbe8e8;
                padding: 0 14px;
                color: #0c6d6d;
                font-size: 12px;
                font-weight: 800;
            }
            QFrame#Card {
                background: #ffffff;
                border: 1px solid #e9e3e2;
                border-radius: 18px;
            }
            QLabel#MutedTinyLabel {
                font-size: 11px;
                color: #96a4a6;
                font-weight: 700;
                letter-spacing: 1px;
            }
            QTextEdit#InputEditor, QPlainTextEdit#LogEdit {
                background: #f7f3f2;
                border: 1px solid #ece4e3;
                border-radius: 14px;
                padding: 14px;
                color: #1f2426;
                font-size: 13px;
            }
            QTextEdit#InputEditor:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 1px solid #7dd6d4;
            }
            QPushButton#PrimaryButton, QPushButton#SecondaryButton, QPushButton#GhostButton, QPushButton#LinkButton {
                border-radius: 12px;
                padding: 12px 18px;
                font-size: 13px;
                font-weight: 800;
            }
            QPushButton#PrimaryButton {
                border: none;
                background: #0c7b7b;
                color: white;
            }
            QPushButton#PrimaryButton:hover {
                background: #0a6f6f;
            }
            QPushButton#SecondaryButton {
                border: none;
                background: #dff4f4;
                color: #0c6d6d;
            }
            QPushButton#SecondaryButton:hover {
                background: #cfeeee;
            }
            QPushButton#GhostButton {
                border: 1px solid #e4dddc;
                background: white;
                color: #445354;
            }
            QPushButton#GhostButton:hover {
                background: #f7f2f1;
            }
            QPushButton#LinkButton {
                border: none;
                background: transparent;
                color: #0c7b7b;
                padding-left: 8px;
                padding-right: 8px;
            }
            QPushButton#LinkButton:hover {
                background: #eef9f9;
            }
            QPushButton:disabled {
                background: #ece7e6;
                color: #9aa5a6;
                border-color: #ece7e6;
            }
            QLabel#EmptyIcon {
                background: #f4fbfb;
                color: #9bb4b4;
                border: 1px solid #dcebec;
                border-radius: 41px;
                font-size: 34px;
                font-weight: 800;
            }
            QLabel#EmptyTitle {
                font-size: 18px;
                font-weight: 800;
                color: #223133;
            }
            QLabel#EmptySubtitle {
                font-size: 13px;
                color: #728183;
            }
            QTableWidget#DataTable {
                background: white;
                border: 1px solid #eee7e5;
                border-radius: 14px;
                alternate-background-color: #fbf9f8;
                color: #1f2426;
                font-size: 13px;
                selection-background-color: transparent;
            }
            QHeaderView::section {
                background: #f7f3f2;
                color: #5a696b;
                border: none;
                border-bottom: 1px solid #e7dfde;
                padding: 12px 10px;
                font-size: 12px;
                font-weight: 800;
            }
            QTableWidget::item {
                border-bottom: 1px solid #f0e9e8;
                padding: 10px;
            }
            QProgressBar#SummaryProgress {
                min-height: 18px;
                border-radius: 9px;
                background: #ebe6e5;
                border: none;
            }
            QProgressBar#SummaryProgress::chunk {
                border-radius: 9px;
                background: #0c7b7b;
            }
            QLabel#SummaryTitle {
                font-size: 14px;
                font-weight: 800;
                color: #223133;
            }
            QLabel#SummaryDetail {
                font-size: 12px;
                color: #637274;
            }
            QFrame#FooterBar {
                background: #f7f2f1;
                border: 1px solid #ebe4e3;
                border-radius: 14px;
            }
            QLabel#FooterLabel {
                color: #637274;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#PathCode {
                background: #ffffff;
                border: 1px solid #e9e3e2;
                border-radius: 8px;
                padding: 6px 10px;
                color: #2a3435;
                font-size: 12px;
            }
            QPushButton#InlineLinkButton {
                border: none;
                background: transparent;
                color: #0c7b7b;
                font-size: 12px;
                font-weight: 800;
            }
            QPushButton#InlineLinkButton:hover {
                text-decoration: underline;
            }
            QCheckBox {
                color: #445354;
                font-size: 12px;
                font-weight: 700;
            }
            QDialog {
                background: #f8f4f3;
            }
            QLineEdit, QComboBox, QSpinBox {
                background: white;
                border: 1px solid #e3dcdb;
                border-radius: 10px;
                padding: 10px 12px;
                color: #1f2426;
                min-height: 20px;
            }
            """
        )

    def _toggle_theme_mode(self) -> None:
        mode_order = ["light", "dark", "system"]
        current_index = mode_order.index(self.config.theme_mode) if self.config.theme_mode in mode_order else 0
        self.config.theme_mode = mode_order[(current_index + 1) % len(mode_order)]
        self.config.theme_preference_explicit = True
        save_config(self.paths, self.config)
        self._apply_styles()
        self._push_log(
            f"界面主题已切换为：{_theme_label(self.config.theme_mode)}，当前显示为{_theme_label(self.effective_theme_mode)}。"
        )

    def _poll_system_theme(self) -> None:
        current_system_mode = _detect_system_theme_mode()
        if current_system_mode == self.last_system_theme_mode:
            return

        self.last_system_theme_mode = current_system_mode
        if self.config.theme_mode == "system":
            self._apply_styles()
            self._push_log(f"已跟随系统主题切换为：{_theme_label(self.effective_theme_mode)}。")

    def _open_history_dialog(self) -> None:
        dialog = HistoryDialog(self.paths, self)
        dialog.exec()

    def _open_analytics_dialog(self) -> None:
        dialog = AnalyticsDialog(self.paths, self)
        dialog.exec()

    def _toggle_use_default_dir(self, checked: bool) -> None:
        self.use_default_dir = checked

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.config, self.paths, self)
        if dialog.exec() != QDialog.Accepted:
            return

        previous_theme_mode = self.config.theme_mode
        self.config.default_download_dir = dialog.default_dir
        self.config.browser_preference = dialog.browser_preference
        self.config.concurrent_downloads = dialog.concurrent_downloads
        self.config.concurrent_fragments = dialog.concurrent_fragments
        self.config.theme_mode = dialog.theme_mode
        self.config.theme_preference_explicit = True
        save_config(self.paths, self.config)

        if self.config.default_download_dir:
            self.use_default_dir = True
            self.default_path_checkbox.setChecked(True)

        if previous_theme_mode != self.config.theme_mode:
            self._apply_styles()

        self._refresh_default_path_hint()
        self._refresh_login_status()
        self._push_log("设置已保存。")

    def _start_read_links(self) -> None:
        if self._is_busy():
            QMessageBox.warning(self, "任务进行中", "当前还有任务在执行，请先等待或停止。")
            return

        raw_text = self.input_edit.toPlainText().strip()
        urls = extract_video_urls(raw_text)
        if not urls:
            QMessageBox.critical(self, "没有识别到链接", "请输入至少一个有效的 B 站视频链接或 BV 号。")
            return

        self._clear_tables()
        self.summary_progress.setValue(0)
        self.summary_title_label.setText("正在读取链接")
        self.summary_detail_label.setText(f"共识别到 {len(urls)} 个链接，正在读取标题和规格。")
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
        except Exception as exc:  # noqa: BLE001
            self._push_log("读取链接时出现未处理异常。")
            self._push_log(traceback.format_exc().strip())
            self.ui_queue.put(("error", f"读取链接失败：{exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    def _start_download(self) -> None:
        if self._is_busy():
            QMessageBox.warning(self, "任务进行中", "当前还有任务在执行，请先等待或停止。")
            return

        if not self.video_items:
            QMessageBox.information(self, "还没有可下载内容", "请先读取链接，再开始下载。")
            return

        output_dir = self._resolve_output_dir()
        if output_dir is None:
            return

        valid_items = [item for item in self.video_items if not item.error_message]
        if not valid_items:
            QMessageBox.critical(self, "没有可下载项", "当前列表里没有成功读取到的视频。")
            return

        self.last_output_dir = output_dir
        self.open_folder_button.setEnabled(True)
        self.stop_event.clear()

        for item in valid_items:
            self.item_runtime[item.task_id] = {
                "status": "待下载",
                "detail": "",
                "progress": 0.0,
                "downloaded_text": _initial_downloaded_text(item),
                "speed_text": "",
                "speed_bps": 0.0,
                "eta_text": "",
            }
            self._render_progress_row(item.task_id)

        self.progress_stack.setCurrentIndex(1)
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
        except Exception as exc:  # noqa: BLE001
            self._push_log("下载任务出现未处理异常。")
            self._push_log(traceback.format_exc().strip())
            self.ui_queue.put(("error", f"下载失败：{exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    def _resolve_output_dir(self) -> Path | None:
        if self.use_default_dir and self.config.default_download_dir:
            output_dir = Path(self.config.default_download_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir

        chosen = QFileDialog.getExistingDirectory(
            self,
            "选择本次下载目录",
            self.config.default_download_dir or str(Path.home()),
        )
        if not chosen:
            return None

        output_dir = Path(chosen).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self.config.default_download_dir = str(output_dir)
        self.use_default_dir = True
        self.default_path_checkbox.setChecked(True)
        save_config(self.paths, self.config)
        self._refresh_default_path_hint()
        self._push_log(f"已将首次选择的目录保存为默认下载目录：{output_dir}")
        return output_dir

    def _open_login_browser(self) -> None:
        if self._is_busy():
            QMessageBox.information(self, "请稍等", "请先等待当前任务结束，再打开登录浏览器。")
            return

        try:
            browser = launch_login_browser(self.paths.browser_profile_dir, self.config.browser_preference)
            self._refresh_login_status()
            self._push_log(
                f"已打开登录浏览器：{browser.display_name}。请在新窗口登录 B 站，完成后关闭那个浏览器窗口即可。"
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法打开登录浏览器", str(exc))

    def _stop_current_task(self) -> None:
        self.stop_event.set()
        self._push_log("已请求停止任务，正在等待当前下载线程退出。")

    def _open_last_folder(self) -> None:
        if self.last_output_dir and self.last_output_dir.exists():
            _open_path(self.last_output_dir)

    def _process_ui_queue(self) -> None:
        processed = 0
        try:
            while processed < self.MAX_UI_EVENTS_PER_TICK:
                event, payload = self.ui_queue.get_nowait()
                processed += 1
                if event == "log":
                    self._append_log(str(payload))
                elif event == "error":
                    self._append_log(str(payload))
                    QMessageBox.critical(self, "执行失败", str(payload))
                elif event == "idle":
                    self.worker_thread = None
                    self._set_busy_state(False)
                elif event == "metadata_ready":
                    self._render_metadata(list(payload))
                elif event == "download_done":
                    self._handle_download_done(payload)
        except queue.Empty:
            pass

        self._flush_pending_item_updates()

        if not self.ui_queue.empty():
            QTimer.singleShot(0, self._process_ui_queue)

    def _render_metadata(self, items: list[VideoMetadata]) -> None:
        self.video_items = items
        self.result_table.setRowCount(0)
        self.progress_table.setRowCount(0)
        self.result_row_by_task_id.clear()
        self.progress_row_by_task_id.clear()
        self.item_runtime.clear()

        success_count = 0
        for item in items:
            result_row = self.result_table.rowCount()
            self.result_table.insertRow(result_row)
            self.result_row_by_task_id[item.task_id] = result_row
            self._set_table_row(
                self.result_table,
                result_row,
                [
                    item.title,
                    item.uploader,
                    item.duration_text,
                    item.best_quality_text,
                    item.error_message or item.status,
                ],
            )

            progress_row = self.progress_table.rowCount()
            self.progress_table.insertRow(progress_row)
            self.progress_row_by_task_id[item.task_id] = progress_row
            self._set_table_row(
                self.progress_table,
                progress_row,
                [
                    item.title,
                    item.error_message or "待下载",
                    "--" if item.error_message else "0.0%",
                    "--" if item.error_message else _initial_downloaded_text(item),
                    "--",
                    "--",
                ],
            )

            if not item.error_message:
                success_count += 1
                self.item_runtime[item.task_id] = {
                    "status": "待下载",
                    "detail": "",
                    "progress": 0.0,
                    "downloaded_text": _initial_downloaded_text(item),
                    "speed_text": "",
                    "speed_bps": 0.0,
                    "eta_text": "",
                }

        self.result_stack.setCurrentIndex(1 if items else 0)
        self.progress_stack.setCurrentIndex(1 if items else 0)
        self.download_button.setEnabled(success_count > 0)
        self.summary_progress.setValue(0)
        self.summary_title_label.setText("读取完成")
        self.summary_detail_label.setText(f"共读取 {len(items)} 条，成功 {success_count} 条。")
        self._push_log(f"读取完成，共 {len(items)} 条，成功 {success_count} 条。")

        if build_cookies_from_browser(self.paths.browser_profile_dir, self.config.browser_preference):
            self._push_log("检测到登录资料，读取和下载时会自动尝试带上登录状态。")
        else:
            self._push_log("当前还没有检测到登录态。如果需要更高规格，请先点击“打开登录浏览器”。")

        self._refresh_login_status()

    def _apply_item_update(self, task_id: str, payload: dict, refresh_summary: bool = True) -> None:
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

        result_row = self.result_row_by_task_id.get(task_id)
        if result_row is not None:
            self._set_cell_text(self.result_table, result_row, 4, status if not detail else f"{status} · {detail}")

        self._render_progress_row(task_id)
        if refresh_summary:
            self._refresh_overall_progress()

    def _render_progress_row(self, task_id: str) -> None:
        item = self._find_video_item(task_id)
        row = self.progress_row_by_task_id.get(task_id)
        runtime = self.item_runtime.get(task_id)
        if item is None or row is None or runtime is None:
            return

        display_status = str(runtime["status"])
        if runtime["detail"]:
            display_status = f"{display_status} | {runtime['detail']}"

        values = [
            item.title,
            display_status,
            f"{float(runtime['progress']):.1f}%",
            str(runtime["downloaded_text"] or "--"),
            str(runtime["speed_text"] or "--"),
            str(runtime["eta_text"] or "--"),
        ]
        self._set_table_row(self.progress_table, row, values)

    def _handle_download_done(self, summary: DownloadSummary) -> None:
        self._refresh_overall_progress(force_finished=True)
        self.summary_title_label.setText("下载结束")
        self.summary_detail_label.setText(
            f"成功 {summary.success_count}，失败 {summary.failed_count}，停止 {summary.stopped_count}。"
        )
        history_entries = self._build_history_entries()
        append_history_entries(self.paths, history_entries)
        self._push_log(
            f"下载结束。成功 {summary.success_count}，失败 {summary.failed_count}，停止 {summary.stopped_count}。"
        )
        if history_entries:
            self._push_log(f"本次任务已写入 {len(history_entries)} 条下载历史。")

    def _build_history_entries(self) -> list[DownloadHistoryEntry]:
        if not self.last_output_dir:
            return []

        recorded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entries: list[DownloadHistoryEntry] = []
        for item in self.video_items:
            if item.error_message:
                continue

            runtime = self.item_runtime.get(item.task_id, {})
            status = str(runtime.get("status") or item.status)
            if status not in {"已完成", "失败", "已停止"}:
                continue

            output_path = ""
            if status == "已完成":
                guessed_path = self._guess_downloaded_file_path(item)
                output_path = str(guessed_path) if guessed_path else ""

            entries.append(
                DownloadHistoryEntry(
                    recorded_at=recorded_at,
                    title=item.title,
                    uploader=item.uploader,
                    video_id=item.video_id,
                    source_url=item.normalized_url or item.source_url,
                    quality_text=item.best_quality_text,
                    duration_text=item.duration_text,
                    status=status,
                    output_dir=str(self.last_output_dir),
                    output_path=output_path,
                )
            )
        return entries

    def _guess_downloaded_file_path(self, item: VideoMetadata) -> Path | None:
        if not self.last_output_dir or not self.last_output_dir.exists() or not item.video_id:
            return None

        patterns = [
            f"* [{item.video_id}].mp4",
            f"* [{item.video_id}].mkv",
            f"* [{item.video_id}].webm",
            f"* [{item.video_id}].*",
        ]
        candidates: list[Path] = []
        for pattern in patterns:
            candidates.extend(
                path
                for path in self.last_output_dir.glob(pattern)
                if path.is_file() and path.suffix.lower() not in {".part", ".ytdl"}
            )
            if candidates:
                break

        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _refresh_overall_progress(self, force_finished: bool = False) -> None:
        valid_items = [item for item in self.video_items if not item.error_message]
        total_items = len(valid_items)
        if total_items == 0:
            self.summary_progress.setValue(0)
            self.summary_title_label.setText("未开始")
            self.summary_detail_label.setText("总速度 0 B/s | 活跃下载 0 个")
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

        fraction = progress_units / max(total_items, 1)
        self.summary_progress.setValue(min(1000, int(fraction * 1000)))
        self.summary_title_label.setText(f"下载中，已完成 {completed_count}/{total_items}")

        detail_parts = [
            f"总速度 {_format_speed(total_speed_bps)}",
            f"活跃下载 {active_count} 个",
        ]
        if merging_count:
            detail_parts.append(f"合并中 {merging_count} 个")
        waiting_count = max(total_items - completed_count - active_count - merging_count, 0)
        if waiting_count:
            detail_parts.append(f"等待中 {waiting_count} 个")
        self.summary_detail_label.setText(" | ".join(detail_parts))

    def _refresh_default_path_hint(self) -> None:
        if self.config.default_download_dir:
            self.default_path_value_label.setText(self.config.default_download_dir)
            self.default_path_checkbox.setEnabled(True)
            return

        self.default_path_value_label.setText("尚未设置，首次下载时会要求你选择目录")
        self.default_path_checkbox.setEnabled(False)
        self.use_default_dir = False
        self.default_path_checkbox.setChecked(False)

    def _refresh_login_status(self) -> None:
        has_login_profile = build_cookies_from_browser(self.paths.browser_profile_dir, self.config.browser_preference)
        if has_login_profile:
            self.login_status_label.setText("已检测到登录资料，可尝试读取更高可访问规格")
        else:
            self.login_status_label.setText("尚未检测到登录资料，建议先登录 B 站账号")

    def _refresh_system_status(self) -> None:
        if is_ffmpeg_available():
            self.system_status_chip.setText("系统就绪")
        else:
            self.system_status_chip.setText("缺少 FFmpeg")

    def _set_busy_state(self, busy: bool) -> None:
        self.read_button.setEnabled(not busy)
        self.download_button.setEnabled((not busy) and self._has_downloadable_items())
        self.stop_button.setEnabled(busy)
        self.login_button.setEnabled(not busy)
        self.sidebar_login_button.setEnabled(not busy)
        self.open_folder_button.setEnabled((not busy) and self.last_output_dir is not None)

    def _clear_tables(self) -> None:
        self.video_items = []
        self.result_row_by_task_id.clear()
        self.progress_row_by_task_id.clear()
        self.item_runtime.clear()
        self.result_table.setRowCount(0)
        self.progress_table.setRowCount(0)
        self.result_stack.setCurrentIndex(0)
        self.progress_stack.setCurrentIndex(0)

    def _find_video_item(self, task_id: str) -> VideoMetadata | None:
        for item in self.video_items:
            if item.task_id == task_id:
                return item
        return None

    def _set_table_row(self, table: QTableWidget, row: int, values: list[str]) -> None:
        for column, value in enumerate(values):
            self._set_cell_text(table, row, column, value)

    def _set_cell_text(self, table: QTableWidget, row: int, column: int, value: str) -> None:
        item = table.item(row, column)
        if item is None:
            item = QTableWidgetItem(value)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row, column, item)
        else:
            item.setText(value)

    def _show_placeholder_message(self, text: str) -> None:
        QMessageBox.information(self, APP_NAME, text)

    def _is_busy(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def _has_downloadable_items(self) -> bool:
        return any(not item.error_message for item in self.video_items)

    def _push_log(self, message: str) -> None:
        formatted = f"[{datetime.now():%H:%M:%S}] {message}"
        self._write_log_line(formatted)
        self.ui_queue.put(("log", formatted))

    def _append_log(self, message: str) -> None:
        self.log_edit.appendPlainText(message)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def _queue_item_update(self, task_id: str, payload: dict) -> None:
        with self.pending_item_updates_lock:
            self.pending_item_updates[task_id] = dict(payload)

    def _flush_pending_item_updates(self) -> None:
        with self.pending_item_updates_lock:
            if not self.pending_item_updates:
                return
            pending_updates = self.pending_item_updates
            self.pending_item_updates = {}

        for task_id, payload in pending_updates.items():
            self._apply_item_update(task_id, payload, refresh_summary=False)
        self._refresh_overall_progress()

    def _write_log_line(self, message: str) -> None:
        with self.log_file_lock:
            self.session_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.session_log_path.open("a", encoding="utf-8") as handle:
                handle.write(message)
                handle.write("\n")

    def _set_window_icon(self) -> None:
        for filename in ("app_icon.png", "app_icon.icns", "app_icon.ico"):
            icon_path = _resolve_asset_path(filename)
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
                return


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, paths: AppPaths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.resize(520, 340)

        self.default_dir = config.default_download_dir
        self.browser_preference = config.browser_preference
        self.concurrent_downloads = config.concurrent_downloads
        self.concurrent_fragments = config.concurrent_fragments
        self.theme_mode = config.theme_mode

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(18)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(14)

        grid.addWidget(QLabel("默认下载目录"), 0, 0)
        self.default_dir_edit = QLineEdit(self.default_dir)
        browse_button = QPushButton("选择目录")
        browse_button.setObjectName("GhostButton")
        browse_button.clicked.connect(self._choose_directory)
        grid.addWidget(self.default_dir_edit, 1, 0)
        grid.addWidget(browse_button, 1, 1)

        grid.addWidget(QLabel("登录浏览器"), 2, 0)
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(["auto", "chrome", "edge"])
        self.browser_combo.setCurrentText(self.browser_preference or "auto")
        grid.addWidget(self.browser_combo, 3, 0)

        grid.addWidget(QLabel("界面主题"), 2, 1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("跟随系统", "system")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        theme_index = {"system": 0, "light": 1, "dark": 2}.get(self.theme_mode, 0)
        self.theme_combo.setCurrentIndex(theme_index)
        grid.addWidget(self.theme_combo, 3, 1)

        grid.addWidget(QLabel("同时下载数量"), 4, 0)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 4)
        self.parallel_spin.setValue(self.concurrent_downloads)
        grid.addWidget(self.parallel_spin, 5, 0)

        grid.addWidget(QLabel("单任务加速线程"), 4, 1)
        self.fragment_spin = QSpinBox()
        self.fragment_spin.setRange(1, 8)
        self.fragment_spin.setValue(self.concurrent_fragments)
        grid.addWidget(self.fragment_spin, 5, 1)

        layout.addLayout(grid)
        layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton("取消")
        cancel_button.setObjectName("GhostButton")
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("保存")
        save_button.setObjectName("PrimaryButton")
        save_button.clicked.connect(self._accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(save_button)
        layout.addLayout(button_row)

    def _choose_directory(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "选择默认下载目录",
            self.default_dir_edit.text().strip() or str(Path.home()),
        )
        if chosen:
            self.default_dir_edit.setText(chosen)

    def _accept(self) -> None:
        default_dir = self.default_dir_edit.text().strip()
        if default_dir:
            default_path = Path(default_dir).resolve()
            default_path.mkdir(parents=True, exist_ok=True)
            self.default_dir = str(default_path)
        else:
            self.default_dir = ""

        self.browser_preference = self.browser_combo.currentText()
        self.concurrent_downloads = int(self.parallel_spin.value())
        self.concurrent_fragments = int(self.fragment_spin.value())
        self.theme_mode = str(self.theme_combo.currentData() or "light")
        self.accept()


class HistoryDialog(QDialog):
    def __init__(self, paths: AppPaths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.all_entries: list[DownloadHistoryEntry] = []
        self.visible_entries: list[DownloadHistoryEntry] = []

        self.setWindowTitle("下载历史")
        self.setModal(True)
        self.resize(980, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QLabel("下载历史")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题、作者、BV号、链接或保存位置")
        self.search_edit.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self.search_edit, 1)

        self.status_filter_combo = QComboBox()
        self.status_filter_combo.addItem("全部状态", "")
        self.status_filter_combo.addItem("已完成", "已完成")
        self.status_filter_combo.addItem("失败", "失败")
        self.status_filter_combo.addItem("已停止", "已停止")
        self.status_filter_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self.status_filter_combo)

        layout.addLayout(filter_row)

        self.history_table = QTableWidget(0, 6)
        self.history_table.setObjectName("DataTable")
        self.history_table.setHorizontalHeaderLabels(["时间", "标题", "状态", "规格", "时长", "保存位置"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setShowGrid(False)
        self.history_table.setWordWrap(False)
        self.history_table.setFocusPolicy(Qt.NoFocus)
        self.history_table.itemSelectionChanged.connect(self._update_action_state)
        header = self.history_table.horizontalHeader()
        header.setStretchLastSection(True)
        for index, width in enumerate([150, 280, 90, 150, 90, 260]):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
            self.history_table.setColumnWidth(index, width)
        layout.addWidget(self.history_table, 1)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)

        self.open_file_button = QPushButton("打开文件")
        self.open_file_button.setObjectName("SecondaryButton")
        self.open_file_button.clicked.connect(self._open_selected_file)
        button_row.addWidget(self.open_file_button)

        self.open_folder_button = QPushButton("打开所在目录")
        self.open_folder_button.setObjectName("GhostButton")
        self.open_folder_button.clicked.connect(self._open_selected_folder)
        button_row.addWidget(self.open_folder_button)

        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("GhostButton")
        refresh_button.clicked.connect(self.reload_entries)
        button_row.addWidget(refresh_button)

        clear_button = QPushButton("清空历史")
        clear_button.setObjectName("GhostButton")
        clear_button.clicked.connect(self._clear_history)
        button_row.addWidget(clear_button)

        button_row.addStretch(1)

        close_button = QPushButton("关闭")
        close_button.setObjectName("PrimaryButton")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)

        layout.addLayout(button_row)

        self.reload_entries()

    def reload_entries(self) -> None:
        self.all_entries = load_history(self.paths)
        self._apply_filters()

    def _apply_filters(self) -> None:
        keyword = self.search_edit.text().strip().lower()
        status_filter = str(self.status_filter_combo.currentData() or "")
        self.visible_entries = []
        self.history_table.setRowCount(0)
        for entry in self.all_entries:
            if status_filter and entry.status != status_filter:
                continue

            haystack = " ".join(
                [
                    entry.title,
                    entry.uploader,
                    entry.video_id,
                    entry.source_url,
                    entry.output_dir,
                    entry.quality_text,
                    entry.status,
                ]
            ).lower()
            if keyword and keyword not in haystack:
                continue

            self.visible_entries.append(entry)

        for row_index, entry in enumerate(self.visible_entries):
            self.history_table.insertRow(row_index)
            values = [
                entry.recorded_at,
                entry.title,
                entry.status,
                entry.quality_text,
                entry.duration_text,
                entry.output_dir,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.history_table.setItem(row_index, column, item)

        if self.visible_entries:
            self.history_table.selectRow(0)
        self._update_action_state()

    def _selected_entry(self) -> DownloadHistoryEntry | None:
        row = self.history_table.currentRow()
        if row < 0 or row >= len(self.visible_entries):
            return None
        return self.visible_entries[row]

    def _update_action_state(self) -> None:
        entry = self._selected_entry()
        file_path = resolve_existing_path(entry.output_path) if entry else None
        folder_path = resolve_existing_path(entry.output_dir) if entry else None
        self.open_file_button.setEnabled(file_path is not None)
        self.open_folder_button.setEnabled(folder_path is not None)

    def _open_selected_file(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return

        file_path = resolve_existing_path(entry.output_path)
        if file_path is None:
            QMessageBox.information(self, APP_NAME, "这条历史记录暂时没有找到对应的本地文件。")
            return
        _open_path(file_path)

    def _open_selected_folder(self) -> None:
        entry = self._selected_entry()
        if not entry:
            return

        folder_path = resolve_existing_path(entry.output_dir)
        if folder_path is None:
            QMessageBox.information(self, APP_NAME, "这条历史记录对应的目录不存在了。")
            return
        _open_path(folder_path)

    def _clear_history(self) -> None:
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "确定要清空全部下载历史吗？这个操作不会删除已经下载的视频文件。",
        )
        if answer != QMessageBox.Yes:
            return

        clear_history(self.paths)
        self.reload_entries()


class AnalyticsDialog(QDialog):
    def __init__(self, paths: AppPaths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.entries = load_history(paths)

        self.setWindowTitle("数据统计")
        self.setModal(True)
        self.resize(980, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QLabel("数据统计")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(12)
        summary_grid.setVerticalSpacing(12)
        stats = _build_history_stats(self.entries)

        cards = [
            ("历史总数", str(stats["total_count"])),
            ("成功下载", str(stats["success_count"])),
            ("失败任务", str(stats["failed_count"])),
            ("停止任务", str(stats["stopped_count"])),
            ("今日新增", str(stats["today_count"])),
            ("近 7 天", str(stats["last_7_days_count"])),
            ("涉及作者", str(stats["unique_uploader_count"])),
            ("保存目录", str(stats["unique_output_dir_count"])),
        ]
        for index, (label_text, value_text) in enumerate(cards):
            summary_grid.addWidget(self._build_stat_card(label_text, value_text), index // 4, index % 4)

        layout.addLayout(summary_grid)

        self.top_uploader_container, self.top_uploader_table = self._build_counter_table("高频作者", ["作者", "次数"])
        self.quality_container, self.quality_table = self._build_counter_table("规格分布", ["规格", "次数"])

        self._fill_counter_table(self.top_uploader_table, stats["top_uploaders"])
        self._fill_counter_table(self.quality_table, stats["top_qualities"])

        layout.addWidget(self.top_uploader_container, 1)
        layout.addWidget(self.quality_container, 1)

        close_button = QPushButton("关闭")
        close_button.setObjectName("PrimaryButton")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, 0, Qt.AlignRight)

    def _build_stat_card(self, label_text: str, value_text: str) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        card.setMinimumHeight(96)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(8)

        label = QLabel(label_text)
        label.setObjectName("FooterLabel")
        value = QLabel(value_text)
        value.setObjectName("HeroTitle")
        value.setStyleSheet("font-size: 22px; font-weight: 900;")

        card_layout.addWidget(label)
        card_layout.addWidget(value)
        card_layout.addStretch(1)
        return card

    def _build_counter_table(self, title_text: str, headers: list[str]) -> tuple[QWidget, QTableWidget]:
        container = QFrame()
        container.setObjectName("Card")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title = QLabel(title_text)
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        table = QTableWidget(0, len(headers))
        table.setObjectName("DataTable")
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setMinimumHeight(180)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        table.setColumnWidth(0, 540)
        table.setColumnWidth(1, 120)

        layout.addWidget(table)
        return container, table

    def _fill_counter_table(self, table: QTableWidget, rows: list[tuple[str, int]]) -> None:
        table.setRowCount(0)
        for row_index, (name, count) in enumerate(rows):
            table.insertRow(row_index)
            for column, value in enumerate([name, str(count)]):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(row_index, column, item)


def _build_history_stats(entries: list[DownloadHistoryEntry]) -> dict[str, object]:
    now = datetime.now()
    today = now.date()
    seven_days_ago = now - timedelta(days=7)

    success_count = sum(1 for entry in entries if entry.status == "已完成")
    failed_count = sum(1 for entry in entries if entry.status == "失败")
    stopped_count = sum(1 for entry in entries if entry.status == "已停止")

    today_count = 0
    last_7_days_count = 0
    uploader_counter: Counter[str] = Counter()
    quality_counter: Counter[str] = Counter()
    unique_output_dirs = {entry.output_dir for entry in entries if entry.output_dir}

    for entry in entries:
        parsed_at = _parse_history_time(entry.recorded_at)
        if parsed_at is not None:
            if parsed_at.date() == today:
                today_count += 1
            if parsed_at >= seven_days_ago:
                last_7_days_count += 1

        if entry.uploader:
            uploader_counter[entry.uploader] += 1
        if entry.quality_text:
            quality_counter[entry.quality_text] += 1

    return {
        "total_count": len(entries),
        "success_count": success_count,
        "failed_count": failed_count,
        "stopped_count": stopped_count,
        "today_count": today_count,
        "last_7_days_count": last_7_days_count,
        "unique_uploader_count": len(uploader_counter),
        "unique_output_dir_count": len(unique_output_dirs),
        "top_uploaders": uploader_counter.most_common(8),
        "top_qualities": quality_counter.most_common(8),
    }


def _parse_history_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


THEME_COLORS: dict[str, dict[str, str]] = {
    "light": {
        "root_bg": "#f3efee",
        "sidebar_bg": "#fbf8f7",
        "sidebar_border": "#e7dfde",
        "sidebar_hover": "#efebea",
        "card_bg": "#ffffff",
        "card_border": "#e9e3e2",
        "topbar_bg": "#f7f2f1",
        "topbar_border": "#ece4e3",
        "text_primary": "#1f2426",
        "text_heading": "#171c1d",
        "text_secondary": "#637274",
        "text_soft": "#445354",
        "text_muted": "#96a4a6",
        "primary": "#0c7b7b",
        "primary_hover": "#0a6f6f",
        "primary_soft_bg": "#dff4f4",
        "primary_soft_hover": "#cfeeee",
        "primary_soft_text": "#0c6d6d",
        "chip_bg": "#f6fbfb",
        "chip_border": "#dbe8e8",
        "chip_text": "#0c6d6d",
        "input_bg": "#f7f3f2",
        "input_border": "#ece4e3",
        "input_focus": "#7dd6d4",
        "ghost_bg": "#ffffff",
        "ghost_border": "#e4dddc",
        "ghost_hover": "#f7f2f1",
        "empty_bg": "#f4fbfb",
        "empty_border": "#dcebec",
        "empty_text": "#9bb4b4",
        "table_bg": "#ffffff",
        "table_alt_bg": "#fbf9f8",
        "table_header_bg": "#f7f3f2",
        "table_header_text": "#5a696b",
        "table_row_border": "#f0e9e8",
        "progress_bg": "#ebe6e5",
        "footer_bg": "#f7f2f1",
        "footer_border": "#ebe4e3",
        "path_bg": "#ffffff",
        "path_border": "#e9e3e2",
        "dialog_bg": "#f8f4f3",
        "disabled_bg": "#ece7e6",
        "disabled_text": "#9aa5a6",
        "link_hover_bg": "#eef9f9",
        "scroll_track": "#efe8e6",
        "scroll_thumb": "#c4d0d1",
        "scroll_thumb_hover": "#acbbbb",
    },
    "dark": {
        "root_bg": "#0f1417",
        "sidebar_bg": "#12181c",
        "sidebar_border": "#273238",
        "sidebar_hover": "#1a2328",
        "card_bg": "#171f24",
        "card_border": "#29343a",
        "topbar_bg": "#131b20",
        "topbar_border": "#273238",
        "text_primary": "#eef4f5",
        "text_heading": "#f7fbfb",
        "text_secondary": "#9dafb4",
        "text_soft": "#c7d3d6",
        "text_muted": "#72858a",
        "primary": "#33bdbd",
        "primary_hover": "#29adad",
        "primary_soft_bg": "#17383b",
        "primary_soft_hover": "#1d4548",
        "primary_soft_text": "#9ceaea",
        "chip_bg": "#163033",
        "chip_border": "#295356",
        "chip_text": "#8fe3e3",
        "input_bg": "#11181c",
        "input_border": "#28343a",
        "input_focus": "#53c5c4",
        "ghost_bg": "#1a2227",
        "ghost_border": "#314047",
        "ghost_hover": "#222c31",
        "empty_bg": "#163033",
        "empty_border": "#295356",
        "empty_text": "#6db9b9",
        "table_bg": "#141b20",
        "table_alt_bg": "#182127",
        "table_header_bg": "#1b252a",
        "table_header_text": "#91a5a9",
        "table_row_border": "#253037",
        "progress_bg": "#273136",
        "footer_bg": "#141d22",
        "footer_border": "#273238",
        "path_bg": "#10181b",
        "path_border": "#273238",
        "dialog_bg": "#12191d",
        "disabled_bg": "#2a353a",
        "disabled_text": "#6f8186",
        "link_hover_bg": "#173235",
        "scroll_track": "#162024",
        "scroll_thumb": "#33454b",
        "scroll_thumb_hover": "#43616a",
    },
}


def _theme_colors(theme_mode: str) -> dict[str, str]:
    return THEME_COLORS.get(theme_mode, THEME_COLORS["light"])


def _theme_label(theme_mode: str) -> str:
    if theme_mode == "dark":
        return "深色"
    if theme_mode == "system":
        return "跟随系统"
    return "浅色"


def _resolve_effective_theme_mode(theme_mode: str) -> str:
    if theme_mode == "system":
        return _detect_system_theme_mode()
    return theme_mode if theme_mode in {"light", "dark"} else "light"


def _detect_system_theme_mode() -> str:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip().lower() == "dark":
                return "dark"
            return "light"
        except Exception:
            pass

    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) else "dark"
        except Exception:
            pass

    app = QApplication.instance()
    if app is not None:
        return "dark" if app.palette().window().color().lightness() < 128 else "light"
    return "light"


def _build_palette(colors: dict[str, str]) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(colors["root_bg"]))
    palette.setColor(QPalette.WindowText, QColor(colors["text_primary"]))
    palette.setColor(QPalette.Base, QColor(colors["input_bg"]))
    palette.setColor(QPalette.AlternateBase, QColor(colors["table_alt_bg"]))
    palette.setColor(QPalette.Text, QColor(colors["text_primary"]))
    palette.setColor(QPalette.Button, QColor(colors["ghost_bg"]))
    palette.setColor(QPalette.ButtonText, QColor(colors["text_soft"]))
    palette.setColor(QPalette.Highlight, QColor(colors["primary"]))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.PlaceholderText, QColor(colors["text_muted"]))
    palette.setColor(QPalette.ToolTipBase, QColor(colors["card_bg"]))
    palette.setColor(QPalette.ToolTipText, QColor(colors["text_primary"]))
    return palette


def _build_stylesheet(colors: dict[str, str]) -> str:
    return """
    QWidget#Root {{
        background: {root_bg};
        color: {text_primary};
    }}
    QFrame#Sidebar {{
        background: {sidebar_bg};
        border-right: 1px solid {sidebar_border};
    }}
    QLabel#SidebarTitle {{
        font-size: 22px;
        font-weight: 800;
        color: {text_primary};
    }}
    QLabel#SidebarSubtitle {{
        font-size: 11px;
        letter-spacing: 1px;
        color: {text_muted};
    }}
    QPushButton#SidebarNavButton, QPushButton#SidebarNavSelected {{
        border: none;
        border-radius: 10px;
        text-align: left;
        padding: 12px 14px;
        font-size: 14px;
        font-weight: 700;
    }}
    QPushButton#SidebarNavButton {{
        background: transparent;
        color: {text_soft};
    }}
    QPushButton#SidebarNavButton:hover {{
        background: {sidebar_hover};
    }}
    QPushButton#SidebarNavSelected {{
        background: {primary};
        color: white;
    }}
    QFrame#SidebarCard {{
        background: {card_bg};
        border: 1px solid {sidebar_border};
        border-radius: 14px;
    }}
    QLabel#CardTitle {{
        font-size: 15px;
        font-weight: 800;
        color: {text_primary};
    }}
    QLabel#SidebarMeta {{
        color: {text_secondary};
        font-size: 12px;
    }}
    QPushButton#SidebarActionButton {{
        border: none;
        border-radius: 10px;
        background: {primary_soft_bg};
        color: {primary_soft_text};
        padding: 10px 12px;
        font-size: 13px;
        font-weight: 700;
    }}
    QPushButton#SidebarActionButton:hover {{
        background: {primary_soft_hover};
    }}
    QPushButton#SidebarFooterButton {{
        border: none;
        border-radius: 10px;
        background: transparent;
        color: {text_soft};
        padding: 10px 12px;
        text-align: left;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#SidebarFooterButton:hover {{
        background: {sidebar_hover};
    }}
    QFrame#TopBar {{
        background: {topbar_bg};
        border-bottom: 1px solid {topbar_border};
    }}
    QLabel#TopBrand {{
        font-size: 16px;
        font-weight: 800;
        color: {text_primary};
    }}
    QPushButton#TopTabButton, QPushButton#TopTabSelected {{
        border: none;
        background: transparent;
        padding: 8px 0;
        font-size: 13px;
        font-weight: 700;
        color: {text_secondary};
    }}
    QPushButton#TopTabButton:hover {{
        color: {text_heading};
    }}
    QPushButton#TopTabSelected {{
        color: {primary};
        border-bottom: 2px solid {primary};
    }}
    QToolButton#RoundButton {{
        border: 1px solid {ghost_border};
        background: {ghost_bg};
        border-radius: 18px;
        padding: 7px 12px;
        font-size: 12px;
        font-weight: 700;
        color: {text_soft};
    }}
    QToolButton#RoundButton:hover {{
        background: {ghost_hover};
    }}
    QLabel#HeroTitle {{
        font-size: 28px;
        font-weight: 900;
        color: {text_heading};
    }}
    QLabel#HeroSubtitle {{
        font-size: 13px;
        color: {text_secondary};
    }}
    QLabel#SystemChip {{
        border-radius: 17px;
        background: {chip_bg};
        border: 1px solid {chip_border};
        padding: 0 14px;
        color: {chip_text};
        font-size: 12px;
        font-weight: 800;
    }}
    QFrame#Card {{
        background: {card_bg};
        border: 1px solid {card_border};
        border-radius: 18px;
    }}
    QLabel#MutedTinyLabel {{
        font-size: 11px;
        color: {text_muted};
        font-weight: 700;
        letter-spacing: 1px;
    }}
    QTextEdit#InputEditor, QPlainTextEdit#LogEdit {{
        background: {input_bg};
        border: 1px solid {input_border};
        border-radius: 14px;
        padding: 14px;
        color: {text_primary};
        font-size: 13px;
    }}
    QTextEdit#InputEditor:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {input_focus};
    }}
    QPushButton#PrimaryButton, QPushButton#SecondaryButton, QPushButton#GhostButton, QPushButton#LinkButton {{
        border-radius: 12px;
        padding: 12px 18px;
        font-size: 13px;
        font-weight: 800;
    }}
    QPushButton#PrimaryButton {{
        border: none;
        background: {primary};
        color: white;
    }}
    QPushButton#PrimaryButton:hover {{
        background: {primary_hover};
    }}
    QPushButton#SecondaryButton {{
        border: none;
        background: {primary_soft_bg};
        color: {primary_soft_text};
    }}
    QPushButton#SecondaryButton:hover {{
        background: {primary_soft_hover};
    }}
    QPushButton#GhostButton {{
        border: 1px solid {ghost_border};
        background: {ghost_bg};
        color: {text_soft};
    }}
    QPushButton#GhostButton:hover {{
        background: {ghost_hover};
    }}
    QPushButton#LinkButton {{
        border: none;
        background: transparent;
        color: {primary};
        padding-left: 8px;
        padding-right: 8px;
    }}
    QPushButton#LinkButton:hover {{
        background: {link_hover_bg};
    }}
    QPushButton:disabled {{
        background: {disabled_bg};
        color: {disabled_text};
        border-color: {disabled_bg};
    }}
    QWidget#EmptyState {{
        background: transparent;
    }}
    QLabel#EmptyIcon {{
        background: {empty_bg};
        color: {primary};
        border: 1px solid {empty_border};
        border-radius: 36px;
        font-size: 30px;
        font-weight: 800;
    }}
    QLabel#EmptyTitle {{
        font-size: 16px;
        font-weight: 800;
        color: {text_heading};
    }}
    QLabel#EmptySubtitle {{
        font-size: 13px;
        color: {text_secondary};
        padding: 0 6px;
    }}
    QTableWidget#DataTable {{
        background: {table_bg};
        border: 1px solid {card_border};
        border-radius: 14px;
        alternate-background-color: {table_alt_bg};
        color: {text_primary};
        font-size: 13px;
        selection-background-color: transparent;
    }}
    QHeaderView::section {{
        background: {table_header_bg};
        color: {table_header_text};
        border: none;
        border-bottom: 1px solid {sidebar_border};
        padding: 12px 10px;
        font-size: 12px;
        font-weight: 800;
    }}
    QTableWidget::item {{
        border-bottom: 1px solid {table_row_border};
        padding: 10px;
    }}
    QProgressBar#SummaryProgress {{
        min-height: 18px;
        border-radius: 9px;
        background: {progress_bg};
        border: none;
    }}
    QProgressBar#SummaryProgress::chunk {{
        border-radius: 9px;
        background: {primary};
    }}
    QLabel#SummaryTitle {{
        font-size: 14px;
        font-weight: 800;
        color: {text_heading};
    }}
    QLabel#SummaryDetail {{
        font-size: 12px;
        color: {text_secondary};
    }}
    QFrame#FooterBar {{
        background: {footer_bg};
        border: 1px solid {footer_border};
        border-radius: 14px;
    }}
    QLabel#FooterLabel {{
        color: {text_secondary};
        font-size: 12px;
        font-weight: 700;
    }}
    QLabel#PathCode {{
        background: {path_bg};
        border: 1px solid {path_border};
        border-radius: 8px;
        padding: 6px 10px;
        color: {text_primary};
        font-size: 12px;
    }}
    QPushButton#InlineLinkButton {{
        border: none;
        background: transparent;
        color: {primary};
        font-size: 12px;
        font-weight: 800;
    }}
    QPushButton#InlineLinkButton:hover {{
        text-decoration: underline;
    }}
    QCheckBox {{
        color: {text_soft};
        font-size: 12px;
        font-weight: 700;
    }}
    QDialog {{
        background: {dialog_bg};
    }}
    QLineEdit, QComboBox, QSpinBox {{
        background: {ghost_bg};
        border: 1px solid {input_border};
        border-radius: 10px;
        padding: 10px 12px;
        color: {text_primary};
        min-height: 20px;
    }}
    QComboBox QAbstractItemView {{
        background: {card_bg};
        border: 1px solid {card_border};
        color: {text_primary};
        selection-background-color: {primary_soft_bg};
        selection-color: {text_primary};
    }}
    QScrollBar:vertical {{
        width: 12px;
        background: {scroll_track};
        border-radius: 6px;
        margin: 4px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {scroll_thumb};
        min-height: 36px;
        border-radius: 6px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {scroll_thumb_hover};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
        background: transparent;
    }}
    """.format(**colors)


def _initial_downloaded_text(item: VideoMetadata) -> str:
    if item.estimated_total_bytes > 0:
        return f"0 B / {_format_bytes(item.estimated_total_bytes)}"
    return "0 B"


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


def _preferred_ui_font_family() -> str:
    if sys.platform == "darwin":
        return "PingFang SC"
    return "Microsoft YaHei UI"


def _open_path(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


def _resolve_asset_path(filename: str) -> Path:
    return Path(__file__).resolve().parents[3] / "assets" / filename
