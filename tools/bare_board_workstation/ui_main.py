from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk

from .config import CONFIG_PATH, BareBoardConfig, load_config, save_config
from .flash_runner import detect_jlink_probes, file_sha256
from .flow import run_bare_board_test
from .serial_runner import SerialMonitor, list_serial_ports


STEP_LABELS_ZH = {
    "Flash": "SWD 烧录",
    "Wait": "等待启动",
    "Serial": "串口采集",
}

STEP_STATUS_ZH = {
    "RUN": "执行中",
    "PASS": "通过",
    "OK": "通过",
    "WARN": "警告",
    "NG": "失败",
    "FAIL": "失败",
    "ERR": "失败",
    "CANCELLED": "已取消",
}

STEP_STATUS_COLORS = {
    "RUN": "#006DFF",
    "PASS": "#00A63E",
    "OK": "#00A63E",
    "WARN": "#C97800",
    "NG": "#E00000",
    "FAIL": "#E00000",
    "ERR": "#E00000",
    "CANCELLED": "#6B7280",
}

STEP_INDEX = {
    "Flash": 1,
    "Wait": 2,
    "Serial": 3,
}


class BareBoardApp(ttk.Window):
    def __init__(self) -> None:
        super().__init__(themename="flatly")
        self.title("Axi Bare Board Workstation")
        width, height, min_width, min_height = self._window_bounds()
        self.compact_layout = width < 1920 or height < 1080
        self._last_layout_compact = self.compact_layout
        self._initial_window_width = width
        self.geometry(f"{width}x{height}")
        self.minsize(min_width, min_height)

        self.config_path = CONFIG_PATH
        self.config_model = load_config(self.config_path)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.connect_worker: threading.Thread | None = None
        self.probe_worker: threading.Thread | None = None
        self.probe_detecting = False
        self.serial_monitor = SerialMonitor()
        self.stop_event = threading.Event()
        self.busy = False
        self.step_status_labels: dict[str, tk.Label] = {}
        self.step_status_state: dict[str, tuple[str, str]] = {}
        self._step_tree_compact_columns: bool | None = None
        self._step_status_refresh_job: str | None = None

        self._build_vars()
        self._build_style()
        self._build_ui()
        self._refresh_ports()
        self._refresh_flash_text()
        self._set_runtime_status("READY", "就绪")
        self._center_window()
        self._restore_main_sash()
        self.bind("<Configure>", self._on_window_configure)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_events)

    def _window_bounds(self) -> tuple[int, int, int, int]:
        screen_width = max(800, self.winfo_screenwidth())
        screen_height = max(600, self.winfo_screenheight())
        reserve_width = 80 if screen_width >= 1100 else 40
        reserve_height = 80 if screen_height >= 760 else 60
        usable_width = max(720, screen_width - reserve_width)
        usable_height = max(520, screen_height - reserve_height)
        min_width = min(820, usable_width)
        min_height = min(560, usable_height)
        base_width = max(min_width, min(1360, usable_width, int(screen_width * 0.58)))
        base_height = max(min_height, min(920, usable_height, int(screen_height * 0.68)))
        width = min(usable_width, int(base_width * 1.92))
        height = min(usable_height, int(base_height * 1.68))
        width = max(min_width, width)
        height = max(min_height, height)
        return width, height, min_width, min_height

    def _center_window(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2 - 30)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _on_window_configure(self, event) -> None:
        if event.widget is not self:
            return
        self.compact_layout = event.width < 1920 or event.height < 1080
        if self.compact_layout == self._last_layout_compact:
            return
        self._last_layout_compact = self.compact_layout
        self._apply_responsive_layout()

    def _target_left_width(self, total_width: int | None = None) -> int:
        if total_width is None:
            total_width = max(self.winfo_width(), getattr(self, "_initial_window_width", 0), 960)
        target = int(total_width * 0.46)
        if self.compact_layout:
            return max(360, min(560, target))
        return max(520, min(760, target))

    def _apply_responsive_layout(self) -> None:
        left_width = self._target_left_width()
        if hasattr(self, "left_panel"):
            self.left_panel.configure(width=left_width)
        if hasattr(self, "main_panes"):
            self.after_idle(lambda width=left_width: self._set_main_sash(width))
        if hasattr(self, "runtime_status_label"):
            self.runtime_status_label.configure(width=10 if self.compact_layout else 12, padx=8)
        if hasattr(self, "step_tree"):
            self.step_tree.configure(height=6 if self.compact_layout else 8)
            self._configure_step_tree_columns()
            self._schedule_step_status_refresh()
        if hasattr(self, "log_text"):
            self.log_text.configure(height=14 if self.compact_layout else 24, width=42 if self.compact_layout else 50)

    def _set_main_sash(self, left_width: int) -> None:
        try:
            self.main_panes.sashpos(0, left_width)
        except tk.TclError:
            pass

    def _restore_main_sash(self) -> None:
        left_width = self._target_left_width()
        self._set_main_sash(left_width)
        self.after(80, lambda width=left_width: self._set_main_sash(width))

    def _add_scrollable_tab(self, tabs: ttk.Notebook, text: str) -> tuple[ttk.Frame, ttk.Frame]:
        outer = ttk.Frame(tabs)
        tabs.add(outer, text=text)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        content = ttk.Frame(canvas, padding=10)
        window_id = canvas.create_window((0, 0), window=content, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def update_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_content_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def on_mousewheel(event) -> str:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        content.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", fit_content_width)
        outer.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", on_mousewheel))
        outer.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return outer, content

    def _build_vars(self) -> None:
        cfg = self.config_model
        self.port_var = tk.StringVar(value=cfg.serial_port)
        self.baud_var = tk.StringVar(value=str(cfg.serial_baudrate or 115200))
        self.sn_var = tk.StringVar()
        self.station_var = tk.StringVar(value=cfg.station_id or "BARE")
        self.backend_var = tk.StringVar(value=cfg.flash_backend or "nrfjprog")
        self.image_var = tk.StringVar(value=cfg.flash_image_path)
        self.script_var = tk.StringVar(value=cfg.flash_script_path)
        self.repo_var = tk.StringVar(value=cfg.firmware_repo)
        self.probe_var = tk.StringVar(value=cfg.jlink_probe_id)
        self.family_var = tk.StringVar(value=cfg.nrfjprog_family)
        self.nrfjprog_var = tk.StringVar(value=cfg.nrfjprog_path or "nrfjprog")
        self.start_cmd_var = tk.StringVar(value=cfg.test_start_command or "AT+DRVTEST")
        self.start_prompt_patterns_var = tk.StringVar(value="\n".join(cfg.start_prompt_patterns))
        self.start_prompt_timeout_var = tk.StringVar(value=str(cfg.start_prompt_timeout_s or 0))
        self.records_var = tk.StringVar(value=cfg.records_root)
        self.timeout_var = tk.StringVar(value=str(cfg.serial_timeout_s or 90))
        self.wait_var = tk.StringVar(value=str(cfg.flash_after_wait_s or 2))
        self.verify_var = tk.BooleanVar(value=bool(cfg.flash_verify))
        self.runtime_status_var = tk.StringVar(value="就绪")
        self.connection_status_var = tk.StringVar(value="未连接")
        self.probe_status_var = tk.StringVar(value="未检测")
        self.sn_enabled_var = tk.BooleanVar(value=bool(getattr(cfg, "sn_record_enabled", True)))
        self.record_var = tk.StringVar(value="")
        self.flash_hash_var = tk.StringVar(value="")
        self.sn_min_var = tk.StringVar(value=str(cfg.sn_rule.min_len))
        self.sn_max_var = tk.StringVar(value=str(cfg.sn_rule.max_len))
        self.sn_prefix_var = tk.StringVar(value=cfg.sn_rule.prefix)
        self.sn_regex_var = tk.StringVar(value=cfg.sn_rule.regex)
        self.pass_patterns_var = tk.StringVar(value="\n".join(cfg.pass_patterns))
        self.fail_patterns_var = tk.StringVar(value="\n".join(cfg.fail_patterns))
        self.end_patterns_var = tk.StringVar(value="\n".join(cfg.end_patterns))

    def _build_style(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("LogTool.TButton", font=("Microsoft YaHei UI", 10), padding=(10, 5))
        style.configure("Step.Treeview", font=("Microsoft YaHei UI", 11), rowheight=36)
        style.configure("Step.Treeview.Heading", font=("Microsoft YaHei UI", 11, "bold"))
        action_font = ("Microsoft YaHei UI", 12, "bold")
        for bs in ("primary", "info", "success", "warning", "danger"):
            style.configure(f"{bs}.TButton", font=action_font, padding=10)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)
        self._build_connection_bar(root)

        panes = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.main_panes = panes

        left_width = self._target_left_width()
        left = ttk.Frame(panes, padding=(0, 0, 8, 0), width=left_width)
        right = ttk.Frame(panes)
        self.left_panel = left
        left.pack_propagate(False)
        panes.add(left, weight=0)
        panes.add(right, weight=1)

        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self._build_run_tab(self.tabs)
        self._build_flash_tab(self.tabs)
        self._build_settings_tab(self.tabs)
        self._build_help_tab(self.tabs)
        self._build_monitor(right)

    def _build_connection_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X)

        row0 = ttk.Frame(bar)
        row0.pack(fill=tk.X)
        ttk.Label(row0, text="COM").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(row0, textvariable=self.port_var, width=12)
        self.port_combo.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row0, text="波特率").pack(side=tk.LEFT)
        ttk.Entry(row0, textvariable=self.baud_var, width=10).pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row0, text="工位").pack(side=tk.LEFT)
        ttk.Entry(row0, textvariable=self.station_var, width=10).pack(side=tk.LEFT, padx=(6, 0))

        row1 = ttk.Frame(bar)
        row1.pack(fill=tk.X, pady=(8, 0))
        tk.Button(
            row1,
            text="刷新",
            command=self._refresh_ports,
            width=8,
            font=("Microsoft YaHei UI", 10),
            bg="#EEF2F5",
            fg="#4B5563",
            relief=tk.FLAT,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.connect_button = tk.Button(
            row1,
            text="连接",
            command=self._connect,
            width=10,
            font=("Microsoft YaHei UI", 11, "bold"),
            bg="#1ABC9C",
            fg="#FFFFFF",
            activebackground="#16A085",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
        )
        self.connect_button.pack(side=tk.LEFT, padx=(0, 8))
        self.disconnect_button = tk.Button(
            row1,
            text="断开",
            command=self._disconnect,
            width=8,
            font=("Microsoft YaHei UI", 10),
            bg="#95A5A6",
            fg="#FFFFFF",
            activebackground="#7F8C8D",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            state=tk.DISABLED,
        )
        self.disconnect_button.pack(side=tk.LEFT, padx=(0, 8))
        self.connection_status_label = tk.Label(
            row1,
            textvariable=self.connection_status_var,
            font=("Microsoft YaHei UI", 11, "bold"),
            fg="#FFFFFF",
            bg="#6B7280",
            padx=8,
            pady=4,
            relief=tk.SOLID,
            borderwidth=1,
            width=10 if self.compact_layout else 12,
            anchor=tk.CENTER,
        )
        self.connection_status_label.pack(side=tk.LEFT)
        self._set_connection_status("DISCONNECTED", "未连接")

        row2 = ttk.Frame(bar)
        row2.pack(fill=tk.X, pady=(8, 0))
        self.start_button = ttk.Button(row2, text="开始裸板测试", bootstyle="success", command=self._start)
        self.start_button.pack(side=tk.LEFT, padx=(0, 8))
        self.stop_button = ttk.Button(row2, text="停止", bootstyle="secondary", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(0, 8))
        self.runtime_status_label = tk.Label(
            row2,
            textvariable=self.runtime_status_var,
            font=("Microsoft YaHei UI", 11, "bold"),
            fg="#FFFFFF",
            bg="#6B7280",
            padx=8,
            pady=4,
            relief=tk.SOLID,
            borderwidth=1,
            width=10 if self.compact_layout else 12,
            anchor=tk.CENTER,
        )
        self.runtime_status_label.pack(side=tk.LEFT)

    def _build_run_tab(self, tabs: ttk.Notebook) -> None:
        _, frame = self._add_scrollable_tab(tabs, "裸板操作")
        ttk.Label(frame, text="DUT", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        ttk.Label(frame, text="SN").grid(row=1, column=0, sticky=tk.W)
        self.sn_entry = ttk.Entry(frame, textvariable=self.sn_var, width=28)
        self.sn_entry.grid(row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Checkbutton(
            frame,
            text="启用 SN/记录",
            variable=self.sn_enabled_var,
            command=self._sync_sn_controls,
            bootstyle="round-toggle",
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(4, 8))
        ttk.Label(frame, text="工位").grid(row=3, column=0, sticky=tk.W)
        ttk.Entry(frame, textvariable=self.station_var, width=28).grid(row=3, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="启动命令").grid(row=4, column=0, sticky=tk.W)
        ttk.Entry(frame, textvariable=self.start_cmd_var, width=28).grid(row=4, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="串口超时(s)").grid(row=5, column=0, sticky=tk.W)
        ttk.Entry(frame, textvariable=self.timeout_var, width=12).grid(row=5, column=1, sticky=tk.W, pady=3)

        ttk.Separator(frame).grid(row=6, column=0, columnspan=2, sticky=tk.EW, pady=12)
        ttk.Label(frame, text="流程说明", style="Title.TLabel").grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        steps_frame = ttk.Frame(frame)
        steps_frame.grid(row=8, column=0, columnspan=2, sticky=tk.EW)
        for step in (
            "1. 连接 COM 口（可选，预监看串口日志）",
            "2. 校验 SN（启用 SN/记录时）",
            "3. 烧录完成后等待 2s，打开串口并发送 AT+DRVTEST",
            "4. 采集 [DRVTEST] 日志并判定 PASS/FAIL",
            "5. 写入 bare_board_records（启用 SN/记录时）",
        ):
            ttk.Label(steps_frame, text=step, foreground="#6B7280", anchor=tk.W).pack(anchor=tk.W, pady=1)
        ttk.Label(frame, textvariable=self.record_var, foreground="#006DFF", anchor=tk.W).grid(
            row=9, column=0, columnspan=2, sticky=tk.EW, pady=(12, 0)
        )
        frame.columnconfigure(1, weight=1)
        self._sync_sn_controls()

    def _build_flash_tab(self, tabs: ttk.Notebook) -> None:
        _, frame = self._add_scrollable_tab(tabs, "芯片烧录")
        ttk.Label(frame, text="J-Link 烧录", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        ttk.Label(frame, text="烧录方式").grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(frame, textvariable=self.backend_var, values=("nrfjprog", "script"), state="readonly", width=16).grid(
            row=1, column=1, sticky=tk.EW, pady=3
        )
        ttk.Label(frame, text="固件 hex 文件").grid(row=2, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.image_var, width=34).grid(row=2, column=1, sticky=tk.EW, pady=3)
        ttk.Button(frame, text="...", width=3, bootstyle="light", command=self._browse_image).grid(row=2, column=2, padx=(5, 0), pady=3)
        ttk.Label(frame, text="烧录脚本").grid(row=3, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.script_var, width=34).grid(row=3, column=1, sticky=tk.EW, pady=3)
        ttk.Button(frame, text="...", width=3, bootstyle="light", command=self._browse_script).grid(row=3, column=2, padx=(5, 0), pady=3)
        ttk.Label(frame, text="nrfjprog").grid(row=4, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.nrfjprog_var, width=34).grid(row=4, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="J-Link ID").grid(row=5, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.probe_var, width=34).grid(row=5, column=1, sticky=tk.EW, pady=3)
        self.probe_detect_button = ttk.Button(
            frame,
            text="检测烧录器",
            bootstyle="info",
            command=self._detect_jlink_probe,
        )
        self.probe_detect_button.grid(row=5, column=2, padx=(5, 0), pady=3, sticky=tk.EW)
        self.probe_status_label = tk.Label(
            frame,
            textvariable=self.probe_status_var,
            font=("Microsoft YaHei UI", 10, "bold"),
            fg="#FFFFFF",
            bg="#6B7280",
            padx=8,
            pady=4,
            relief=tk.SOLID,
            borderwidth=1,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=320,
        )
        self.probe_status_label.grid(row=6, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self._set_probe_status("UNKNOWN", "未检测")
        ttk.Label(frame, text="Family").grid(row=7, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.family_var, width=34).grid(row=7, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="等待秒数").grid(row=8, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.wait_var, width=12).grid(row=8, column=1, sticky=tk.W, pady=3)
        ttk.Checkbutton(frame, text="烧录后校验", variable=self.verify_var, bootstyle="round-toggle").grid(
            row=9, column=0, columnspan=2, sticky=tk.W, pady=(4, 8)
        )
        ttk.Label(frame, textvariable=self.flash_hash_var, wraplength=300, foreground="#6B7280").grid(
            row=10, column=0, columnspan=3, sticky=tk.W, pady=(0, 8)
        )
        frame.columnconfigure(1, weight=1)

    def _build_settings_tab(self, tabs: ttk.Notebook) -> None:
        _, frame = self._add_scrollable_tab(tabs, "设置")
        rows = [
            ("固件仓库", self.repo_var, self._browse_repo),
            ("记录目录", self.records_var, self._browse_records),
            ("SN 最小", self.sn_min_var, None),
            ("SN 最大", self.sn_max_var, None),
            ("SN 前缀", self.sn_prefix_var, None),
            ("SN 正则", self.sn_regex_var, None),
        ]
        for row, (label, var, browse) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
            ttk.Entry(frame, textvariable=var, width=34).grid(row=row, column=1, sticky=tk.EW, pady=3)
            if browse:
                ttk.Button(frame, text="...", width=3, bootstyle="light", command=browse).grid(row=row, column=2, padx=(5, 0), pady=3)

        pattern_row = len(rows)
        ttk.Label(frame, text="WAIT 规则").grid(row=pattern_row, column=0, sticky=tk.NW, pady=3)
        self.start_prompt_text = tk.Text(frame, height=3, width=34, font=("Consolas", 9))
        self.start_prompt_text.grid(row=pattern_row, column=1, columnspan=2, sticky=tk.EW, pady=3)
        self.start_prompt_text.insert("1.0", self.start_prompt_patterns_var.get())

        ttk.Label(frame, text="WAIT 超时(s)").grid(row=pattern_row + 1, column=0, sticky=tk.W, pady=3)
        ttk.Entry(frame, textvariable=self.start_prompt_timeout_var, width=12).grid(
            row=pattern_row + 1, column=1, sticky=tk.W, pady=3
        )

        ttk.Label(frame, text="PASS 规则").grid(row=pattern_row + 2, column=0, sticky=tk.NW, pady=3)
        self.pass_text = tk.Text(frame, height=3, width=34, font=("Consolas", 9))
        self.pass_text.grid(row=pattern_row + 2, column=1, columnspan=2, sticky=tk.EW, pady=3)
        self.pass_text.insert("1.0", self.pass_patterns_var.get())

        ttk.Label(frame, text="FAIL 规则").grid(row=pattern_row + 3, column=0, sticky=tk.NW, pady=3)
        self.fail_text = tk.Text(frame, height=3, width=34, font=("Consolas", 9))
        self.fail_text.grid(row=pattern_row + 3, column=1, columnspan=2, sticky=tk.EW, pady=3)
        self.fail_text.insert("1.0", self.fail_patterns_var.get())

        ttk.Label(frame, text="END 规则").grid(row=pattern_row + 4, column=0, sticky=tk.NW, pady=3)
        self.end_text = tk.Text(frame, height=3, width=34, font=("Consolas", 9))
        self.end_text.grid(row=pattern_row + 4, column=1, columnspan=2, sticky=tk.EW, pady=3)
        self.end_text.insert("1.0", self.end_patterns_var.get())

        ttk.Button(frame, text="保存设置", bootstyle="success", command=self._save_settings).grid(
            row=pattern_row + 5, column=0, columnspan=3, sticky=tk.EW, pady=(12, 0)
        )
        frame.columnconfigure(1, weight=1)

    def _build_help_tab(self, tabs: ttk.Notebook) -> None:
        _, frame = self._add_scrollable_tab(tabs, "更多")
        ttk.Label(frame, text="使用说明", style="Title.TLabel").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
        ttk.Label(
            frame,
            text=(
                "裸板工站默认对接 POC3 factory drvtest 固件。\n\n"
                "建议配置：\n"
                "- 波特率 115200\n"
                "- 启动命令 AT+DRVTEST\n"
                "- PASS 规则 re:\\[DRVTEST\\]\\[FINAL\\].*overall=PASS\n"
                "- 固件镜像指向 build_factory/merged.hex\n\n"
                "操作提示：\n"
                "- 先选 COM 口，点“连接”预监看串口日志\n"
                "- 正式产测勾选“启用 SN/记录”；联调可取消勾选\n"
                "- 选择固件 hex 时请在文件对话框中选 *.hex 文件，不是文件夹"
            ),
            justify=tk.LEFT,
            foreground="#6B7280",
            anchor=tk.W,
        ).grid(row=1, column=0, sticky=tk.EW)
        frame.columnconfigure(0, weight=1)

    def _build_monitor(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=2)
        parent.rowconfigure(3, weight=4)

        ttk.Label(parent, text="执行步骤", style="Title.TLabel").grid(row=0, column=0, sticky=tk.W)
        step_frame = ttk.Frame(parent)
        step_frame.grid(row=1, column=0, sticky=tk.NSEW, pady=(6, 5))
        self.step_tree = ttk.Treeview(
            step_frame,
            columns=("idx", "step", "status", "detail"),
            show="headings",
            style="Step.Treeview",
            height=6 if self.compact_layout else 8,
        )
        for col, title in (("idx", "#"), ("step", "步骤"), ("status", "状态"), ("detail", "详情")):
            self.step_tree.heading(col, text=title)
        self._configure_step_tree_columns()
        self.step_tree.bind("<Configure>", lambda _e: self._on_step_tree_configure())
        self.step_tree.pack(fill=tk.BOTH, expand=True)

        log_header = ttk.Frame(parent)
        log_header.grid(row=2, column=0, sticky=tk.EW, pady=(8, 0))
        ttk.Label(log_header, text="串口日志", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            log_header,
            text="清空日志",
            command=self._clear_log,
            style="LogTool.TButton",
            bootstyle="secondary-outline",
        ).pack(side=tk.RIGHT)

        log_frame = ttk.Frame(parent)
        log_frame.grid(row=3, column=0, sticky=tk.NSEW, pady=(6, 0))
        self.log_text = tk.Text(
            log_frame,
            height=14 if self.compact_layout else 24,
            width=42 if self.compact_layout else 50,
            wrap=tk.NONE,
            font=("Consolas", 10),
        )
        yscroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        xscroll = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        xscroll.grid(row=1, column=0, sticky=tk.EW)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

    def _configure_step_tree_columns(self) -> None:
        tree_width = self.step_tree.winfo_width()
        compact_columns = self.compact_layout or (tree_width > 1 and tree_width < 620)
        if compact_columns == self._step_tree_compact_columns:
            return
        self._step_tree_compact_columns = compact_columns
        if compact_columns:
            self.step_tree.configure(displaycolumns=("idx", "step", "status"))
            widths = {"idx": (44, 36, False), "step": (210, 150, True), "status": (92, 78, False), "detail": (0, 0, False)}
        else:
            self.step_tree.configure(displaycolumns=("idx", "step", "status", "detail"))
            widths = {"idx": (44, 36, False), "step": (260, 180, True), "status": (100, 82, False), "detail": (420, 160, True)}
        for column, (width, minwidth, stretch) in widths.items():
            self.step_tree.column(column, width=width, minwidth=minwidth, stretch=stretch)

    def _on_step_tree_configure(self) -> None:
        self._configure_step_tree_columns()
        self._schedule_step_status_refresh(60)

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        values = [port.device for port in ports]
        self.port_combo.configure(values=values)
        if not self.port_var.get() and values:
            self.port_var.set(values[0])
        if not ports:
            self._log("WARN", "未枚举到串口；确认 pyserial 已安装且 DUT 已连接")

    def _sync_sn_controls(self) -> None:
        if hasattr(self, "sn_entry"):
            self.sn_entry.configure(state=tk.NORMAL if self.sn_enabled_var.get() else tk.DISABLED)

    def _serial_connected(self) -> bool:
        return self.serial_monitor.is_open()

    def _connect(self) -> None:
        if self.busy:
            return
        if self._serial_connected():
            self._log("INFO", "串口已经连接")
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("串口未配置", "请先在顶部选择 COM 口。")
            return
        try:
            baudrate = int(self.baud_var.get().strip() or "115200")
        except ValueError:
            messagebox.showerror("参数错误", "波特率必须是整数。")
            return
        self._set_connection_status("CONNECTING", "连接中...")
        self.connect_button.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                self.serial_monitor.open(port, baudrate, lambda direction, line: self.events.put(("log", direction, line)))
                self.events.put(("connected", port, baudrate))
            except Exception as exc:
                self.events.put(("log", "ERR", f"连接失败: {exc}"))
                self.events.put(("connection_status", "DISCONNECTED", "未连接"))

        self.connect_worker = threading.Thread(target=worker, daemon=True, name="bare-board-connect")
        self.connect_worker.start()

    def _disconnect(self, silent: bool = False) -> None:
        self.serial_monitor.close()
        self._set_connection_status("DISCONNECTED", "未连接")
        self.connect_button.configure(state=tk.NORMAL)
        self.disconnect_button.configure(state=tk.DISABLED)
        if not silent:
            self._log("INFO", "串口已断开")

    def _set_connection_status(self, state: str, detail: str) -> None:
        state_key = state.upper()
        if state_key == "CONNECTED":
            text = detail or "已连接"
            bg = "#008F3A"
        elif state_key == "CONNECTING":
            text = detail or "连接中..."
            bg = "#006DFF"
        else:
            text = detail or "未连接"
            bg = "#6B7280"
        self.connection_status_var.set(text)
        if hasattr(self, "connection_status_label"):
            self.connection_status_label.configure(bg=bg, fg="#FFFFFF")

    def _set_probe_status(self, state: str, detail: str) -> None:
        state_key = state.upper()
        if state_key == "OK":
            text = detail or "烧录器就绪"
            bg = "#008F3A"
        elif state_key == "WARN":
            text = detail or "需要指定 J-Link ID"
            bg = "#C97800"
        elif state_key in {"RUNNING", "CONNECTING"}:
            text = detail or "检测中..."
            bg = "#006DFF"
        elif state_key in {"NG", "FAIL", "ERR"}:
            text = detail or "未检测到烧录器"
            bg = "#E00000"
        else:
            text = detail or "未检测"
            bg = "#6B7280"
        self.probe_status_var.set(text)
        if hasattr(self, "probe_status_label"):
            self.probe_status_label.configure(bg=bg, fg="#FFFFFF")

    def _sync_probe_detect_button(self) -> None:
        if not hasattr(self, "probe_detect_button"):
            return
        if self.busy or self.probe_detecting:
            self.probe_detect_button.configure(state=tk.DISABLED)
        else:
            self.probe_detect_button.configure(state=tk.NORMAL)

    def _detect_jlink_probe(self) -> None:
        if self.busy or self.probe_detecting:
            return
        config = self._current_config()
        self.probe_detecting = True
        self._set_probe_status("RUNNING", "正在检测 J-Link...")
        self._sync_probe_detect_button()
        self._log("INFO", "开始检测烧录器...")

        def worker() -> None:
            try:
                result = detect_jlink_probes(config)
                self.events.put(("probe_detect_result", result))
            except Exception as exc:
                self.events.put(("probe_detect_error", str(exc)))

        self.probe_worker = threading.Thread(target=worker, daemon=True, name="bare-board-probe-detect")
        self.probe_worker.start()

    def _refresh_flash_text(self) -> None:
        image_text = self.image_var.get().strip()
        if image_text and Path(image_text).exists():
            try:
                digest = file_sha256(image_text)
                size = Path(image_text).stat().st_size
                self.flash_hash_var.set(f"固件：{Path(image_text).name} | size={size} | sha256={digest[:12]}...")
            except Exception as exc:
                self.flash_hash_var.set(f"固件 hash 读取失败：{exc}")
        elif image_text:
            self.flash_hash_var.set(f"固件不存在：{image_text}")
        else:
            self.flash_hash_var.set("尚未选择烧录固件")

    def _current_config(self) -> BareBoardConfig:
        config = load_config(self.config_path)
        config.serial_port = self.port_var.get().strip()
        config.serial_baudrate = int(self.baud_var.get().strip() or "115200")
        config.station_id = self.station_var.get().strip() or "BARE"
        config.flash_backend = self.backend_var.get().strip() or "nrfjprog"
        config.flash_image_path = self.image_var.get().strip()
        config.flash_script_path = self.script_var.get().strip()
        config.firmware_repo = self.repo_var.get().strip()
        config.jlink_probe_id = self.probe_var.get().strip()
        config.nrfjprog_family = self.family_var.get().strip()
        config.nrfjprog_path = self.nrfjprog_var.get().strip() or "nrfjprog"
        config.test_start_command = self.start_cmd_var.get().strip()
        config.start_prompt_patterns = [
            line.strip() for line in self.start_prompt_text.get("1.0", tk.END).splitlines() if line.strip()
        ]
        config.start_prompt_timeout_s = float(self.start_prompt_timeout_var.get().strip() or "0")
        config.records_root = self.records_var.get().strip()
        config.serial_timeout_s = float(self.timeout_var.get().strip() or "90")
        config.flash_after_wait_s = float(self.wait_var.get().strip() or "2")
        config.flash_verify = bool(self.verify_var.get())
        config.sn_rule.min_len = int(self.sn_min_var.get().strip() or "1")
        config.sn_rule.max_len = int(self.sn_max_var.get().strip() or "48")
        config.sn_rule.prefix = self.sn_prefix_var.get().strip()
        config.sn_rule.regex = self.sn_regex_var.get().strip()
        config.pass_patterns = [line.strip() for line in self.pass_text.get("1.0", tk.END).splitlines() if line.strip()]
        config.fail_patterns = [line.strip() for line in self.fail_text.get("1.0", tk.END).splitlines() if line.strip()]
        config.end_patterns = [line.strip() for line in self.end_text.get("1.0", tk.END).splitlines() if line.strip()]
        config.sn_record_enabled = bool(self.sn_enabled_var.get())
        return config

    def _save_settings(self, silent: bool = False) -> None:
        try:
            config = self._current_config()
            save_config(config, self.config_path)
            self.config_model = config
            self._refresh_flash_text()
            self._set_runtime_status("READY", "已保存")
            if not silent:
                self._log("OK", "设置已保存")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        sn_enabled = bool(self.sn_enabled_var.get())
        sn = self.sn_var.get().strip() if sn_enabled else ""
        try:
            config = self._current_config()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if sn_enabled:
            ok, reason = config.validate_sn(sn)
            if not ok:
                messagebox.showerror("SN 错误", reason)
                return
            if not sn:
                messagebox.showerror("SN 不能为空", "当前已启用 SN/记录，请先输入 SN。\n\n临时联调请取消勾选“启用 SN/记录”。")
                return
        else:
            if not messagebox.askyesno(
                "确认空跑测试",
                "当前未启用 SN/记录，本次测试不会校验 SN，也不会写入记录文件。\n\n是否继续？",
            ):
                return
        if not config.serial_port:
            messagebox.showerror("串口未配置", "请先在顶部选择 COM 口。")
            return
        if not config.flash_image_path:
            messagebox.showerror("固件未配置", "请先在“芯片烧录”页选择裸板测试固件 hex 文件。")
            return

        self._save_settings(silent=True)
        if self._serial_connected():
            self._disconnect(silent=True)
            self._log("INFO", "测试开始前已断开预连接串口，烧录后由流程重新打开")
        self.stop_event.clear()
        self._clear_log()
        self._clear_steps()
        self.record_var.set("")
        self._set_busy(True)
        self._set_runtime_status("RUNNING", "测试中")
        self.worker = threading.Thread(
            target=self._run_worker,
            args=(config, sn, sn_enabled),
            daemon=True,
        )
        self.worker.start()

    def _stop(self) -> None:
        self.stop_event.set()
        self._set_runtime_status("RUNNING", "停止中")

    def _run_worker(self, config: BareBoardConfig, sn: str, sn_enabled: bool) -> None:
        def line(direction: str, text: str) -> None:
            self.events.put(("log", direction, text))

        def progress(step: str, status: str, detail: str) -> None:
            self.events.put(("step", step, status, detail))

        outcome = run_bare_board_test(
            config,
            sn,
            line_callback=line,
            progress_callback=progress,
            stop_event=self.stop_event,
            record_enabled=sn_enabled,
        )
        self.events.put(("done", outcome))

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.start_button.configure(state=state)
        self.stop_button.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self.connect_button.configure(state=tk.DISABLED)
            self.disconnect_button.configure(state=tk.DISABLED)
        elif self._serial_connected():
            self.connect_button.configure(state=tk.DISABLED)
            self.disconnect_button.configure(state=tk.NORMAL)
        else:
            self.connect_button.configure(state=tk.NORMAL)
            self.disconnect_button.configure(state=tk.DISABLED)
        self._sync_probe_detect_button()

    def _set_runtime_status(self, state: str, detail: str) -> None:
        state_key = state.upper()
        if state_key == "PASS":
            text = "测试通过"
            bg = "#008F3A"
        elif state_key in {"NG", "FAIL", "ERR"}:
            text = "测试失败"
            bg = "#E00000"
        elif state_key == "RUNNING":
            text = detail or "测试中"
            bg = "#006DFF"
        elif state_key == "CANCELLED":
            text = "已取消"
            bg = "#6B7280"
        else:
            text = detail or "就绪"
            bg = "#6B7280"
        self.runtime_status_var.set(text)
        self.runtime_status_label.configure(bg=bg, fg="#FFFFFF")

    def _clear_steps(self) -> None:
        for item in self.step_tree.get_children():
            self.step_tree.delete(item)
        for label in self.step_status_labels.values():
            label.destroy()
        self.step_status_labels.clear()
        self.step_status_state.clear()
        if self._step_status_refresh_job is not None:
            try:
                self.after_cancel(self._step_status_refresh_job)
            except tk.TclError:
                pass
            self._step_status_refresh_job = None

    def _schedule_step_status_refresh(self, delay_ms: int = 25) -> None:
        if self._step_status_refresh_job is not None:
            try:
                self.after_cancel(self._step_status_refresh_job)
            except tk.TclError:
                pass
        self._step_status_refresh_job = self.after(delay_ms, self._refresh_step_status_labels)

    def _refresh_step_status_labels(self) -> None:
        self._step_status_refresh_job = None
        for iid, (display_status, status_key) in list(self.step_status_state.items()):
            if not self.step_tree.exists(iid):
                label = self.step_status_labels.pop(iid, None)
                if label is not None:
                    label.destroy()
                self.step_status_state.pop(iid, None)
                continue
            label = self.step_status_labels.get(iid)
            if label is None:
                label = tk.Label(
                    self.step_tree,
                    anchor=tk.CENTER,
                    bg="#FFFFFF",
                    font=("Microsoft YaHei UI", 11, "bold"),
                    padx=2,
                )
                label.bind("<Button-1>", lambda _e, row=iid: self.step_tree.selection_set(row))
                self.step_status_labels[iid] = label
            label.configure(text=display_status, fg=STEP_STATUS_COLORS.get(status_key, "#111827"))
            bbox = self.step_tree.bbox(iid, "status")
            if bbox:
                x, y, width, height = bbox
                label.place(x=x + 1, y=y + 1, width=max(0, width - 2), height=max(0, height - 2))
            else:
                label.place_forget()

    def _put_step(self, step: str, status: str, detail: str) -> None:
        idx = STEP_INDEX.get(step, 0)
        if idx <= 0:
            return
        iid = str(idx)
        status_key = status.upper()
        display_step = STEP_LABELS_ZH.get(step, step)
        display_status = STEP_STATUS_ZH.get(status_key, status)
        values = (idx, display_step, "", detail)
        self.step_status_state[iid] = (display_status, status_key)
        if self.step_tree.exists(iid):
            self.step_tree.item(iid, values=values)
        else:
            self.step_tree.insert("", tk.END, iid=iid, values=values)
        self.step_tree.see(iid)
        self._schedule_step_status_refresh()

    def _log(self, level: str, message: str) -> None:
        self.log_text.insert(tk.END, f"[{level}] {message}\n")
        self.log_text.see(tk.END)

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _show_done_popup(self, outcome) -> None:
        sn = self.sn_var.get().strip()
        sn_text = f"\nSN：{sn}" if self.sn_enabled_var.get() and sn else ""
        if outcome.ok:
            messagebox.showinfo("测试通过", f"裸板测试通过。{sn_text}")
            return
        if outcome.result == "CANCELLED":
            messagebox.showwarning("测试取消", f"{outcome.message}{sn_text}")
            return
        messagebox.showerror("测试失败", f"{outcome.message}{sn_text}")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._log(event[1], event[2])
                elif kind == "connected":
                    port, baudrate = event[1], event[2]
                    self._set_connection_status("CONNECTED", f"{port}@{baudrate}")
                    self.connect_button.configure(state=tk.DISABLED)
                    self.disconnect_button.configure(state=tk.NORMAL)
                    self._log("OK", f"串口已连接 {port}@{baudrate}")
                elif kind == "connection_status":
                    self._set_connection_status(event[1], event[2])
                    self.connect_button.configure(state=tk.NORMAL)
                    self.disconnect_button.configure(state=tk.DISABLED)
                elif kind == "probe_detect_result":
                    self.probe_detecting = False
                    result = event[1]
                    self._set_probe_status(result.level, result.message)
                    if result.nrfjprog_version:
                        self._log("INFO", f"nrfjprog: {result.nrfjprog_version}")
                    if result.probe_ids:
                        self._log("INFO", f"检测到 J-Link SN: {', '.join(result.probe_ids)}")
                    elif result.raw_output.strip():
                        self._log("WARN" if result.ok else "ERR", result.raw_output.strip())
                    if len(result.probe_ids) == 1 and not self.probe_var.get().strip():
                        self.probe_var.set(result.probe_ids[0])
                        self._log("INFO", f"已自动填入 J-Link ID: {result.probe_ids[0]}")
                    log_level = "OK" if result.ok and result.level == "OK" else ("WARN" if result.ok else "ERR")
                    self._log(log_level, f"烧录器检测: {result.message}")
                    self._sync_probe_detect_button()
                elif kind == "probe_detect_error":
                    self.probe_detecting = False
                    message = event[1]
                    self._set_probe_status("ERR", f"检测失败: {message}")
                    self._log("ERR", f"烧录器检测失败: {message}")
                    self._sync_probe_detect_button()
                elif kind == "step":
                    self._put_step(event[1], event[2], event[3])
                elif kind == "done":
                    outcome = event[1]
                    self._set_busy(False)
                    self._set_runtime_status(outcome.result, outcome.message)
                    if outcome.record_path:
                        self.record_var.set(outcome.record_path)
                    self._log("OK" if outcome.ok else "ERR", f"流程结束: {outcome.result} {outcome.message}")
                    self._show_done_popup(outcome)
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _resolve_browse_dir(self, *candidates: str) -> str:
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            path = Path(text)
            if path.is_file():
                return str(path.parent)
            if path.is_dir():
                return str(path)
        return str(Path.cwd())

    def _browse_image(self) -> None:
        initialdir = self._resolve_browse_dir(
            self.image_var.get(),
            str(Path(self.repo_var.get().strip()) / "build_factory"),
            self.repo_var.get(),
        )
        path = filedialog.askopenfilename(
            title="选择固件 hex 文件",
            initialdir=initialdir,
            filetypes=[
                ("Intel HEX 文件", "*.hex"),
                ("二进制固件", "*.bin"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.image_var.set(path)
            self._refresh_flash_text()

    def _browse_script(self) -> None:
        initialdir = self._resolve_browse_dir(self.script_var.get(), self.repo_var.get())
        path = filedialog.askopenfilename(
            title="选择烧录脚本",
            initialdir=initialdir,
            filetypes=[
                ("PowerShell 脚本", "*.ps1"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.script_var.set(path)

    def _browse_repo(self) -> None:
        path = filedialog.askdirectory(initialdir=self.repo_var.get() or str(Path.cwd()))
        if path:
            self.repo_var.set(path)

    def _browse_records(self) -> None:
        path = filedialog.askdirectory(initialdir=self.records_var.get() or str(Path.cwd()))
        if path:
            self.records_var.set(path)

    def _on_close(self) -> None:
        self.stop_event.set()
        self._disconnect(silent=True)
        self.destroy()


def main() -> None:
    app = BareBoardApp()
    app.mainloop()
