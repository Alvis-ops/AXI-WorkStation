from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox
from pathlib import Path

import ttkbootstrap as ttk

from .at_client import ATClient
from .at_parser import capture_frame_label, is_capture_frame_line
from .config import (
    WorkstationConfig,
    get_factory_token,
    has_engineer_password,
    load_config,
    redact_sensitive_text,
    save_engineer_password,
    save_factory_token,
    save_config,
    verify_engineer_password,
)
from .flash_flow import flash_payload, precheck_flash_request, probe_at_client, record_flash_step
from .flash_runner import FlashOutcome, file_sha256, run_flash
from .flows import FlowOutcome, run_full_machine, run_half_machine
from .ota_runner import build_ota_command, run_ota
from .storage import NullRunRecord, RunStorage
from .transport_ble import BLEDeviceInfo, BLENusTransport, scan_ble_devices
from .transport_uart import UARTTransport, list_serial_ports

# P1_1 UI smoothness (A1/A2/A4)
UI_EVENT_MAX_DRAIN = 80
UI_LOG_MAX_LINES = 900
# Resize fires many Configure events; settle before relayout to keep drag smooth.
UI_RESIZE_SETTLE_MS = 120
UI_STEP_STATUS_REFRESH_MS = 16
UI_CONTROL_EVENT_KINDS = frozenset(
    {
        "busy",
        "connected",
        "connection_status",
        "connection_lost",
        "step",
        "operator_prompt",
        "popup",
        "flow_done",
        "ble_devices",
    }
)


STEP_LABELS_ZH = {
    "AT probe": "AT 连通检查",
    "Read version": "读取固件版本",
    "Read capability": "读取能力信息",
    "Factory AT capability": "检查工厂 AT 能力",
    "Firmware flash": "固件烧录",
    "Flash reconnect": "烧录后重连",
    "Factory unlock": "解锁工厂模式",
    "Factory lock": "锁回工厂模式",
    "Factory lock cleanup": "失败后锁回工厂模式",
    "Write SN": "写入 SN",
    "Read SN": "读取 SN",
    "SN persistence check": "检查 SN 持久化",
    "Read OTA busy": "检查 OTA 忙碌状态",
    "Power path": "电源通路测试",
    "IMU communication": "IMU 通信检查",
    "Touch communication": "MOMO 芯片通信检查",
    "Charger communication": "充电芯片通信检查",
    "Gauge communication": "电量计通信检查",
    "Flash communication": "Flash 通信检查",
    "PPG communication": "PPG 芯片通信检查",
    "PPG dark capture": "PPG 暗场采集",
    "Touch ISR": "MOMO 触摸中断测试",
    "Touch capture": "MOMO 空采集数据",
    "LRA vibcapture": "LRA 震动采集",
    "PPG reflect capture": "PPG 反射采集",
    "OTA transport check": "检查 OTA 通道",
    "OTA version before": "OTA 前读取版本",
    "OTA busy check": "OTA 前忙碌检查",
    "OTA image check": "检查 OTA 包",
    "OTA disconnect NUS": "断开 BLE NUS",
    "OTA upload": "上传 OTA 包",
    "OTA reconnect NUS": "重连 BLE NUS",
    "OTA state check": "检查 OTA 状态",
    "OTA busy after same-hash": "same-hash 后忙碌检查",
    "OTA reboot wait": "等待 OTA 重启",
    "OTA busy after reboot": "重启后忙碌检查",
    "OTA version after": "OTA 后读取版本",
    "Manual": "手动 AT 指令",
}

STEP_STATUS_ZH = {
    "RUN": "执行中",
    "PASS": "通过",
    "OK": "通过",
    "WARN": "警告",
    "NG": "失败",
    "FAIL": "失败",
    "ERR": "失败",
    "PENDING-HW": "待硬件验证",
}

STEP_STATUS_COLORS = {
    "PASS": "#00A63E",
    "OK": "#00A63E",
    "NG": "#E00000",
    "FAIL": "#E00000",
    "ERR": "#E00000",
}

MOMO_TOUCH_STEPS = {"Touch ISR"}

RECORD_OUTPUT_MODE_LABELS = {
    "unified": "集成记录（单个 unified_log.csv）",
    "split": "分散记录（兼容多文件）",
}


def _record_output_label(mode: str) -> str:
    return RECORD_OUTPUT_MODE_LABELS.get(str(mode).strip().lower(), RECORD_OUTPUT_MODE_LABELS["unified"])


def _record_output_mode(label: str) -> str:
    for mode, text in RECORD_OUTPUT_MODE_LABELS.items():
        if label == text:
            return mode
    return "unified"


class WorkstationApp(ttk.Window):
    def __init__(self) -> None:
        super().__init__(themename="flatly")
        self.title("Axi Factory Workstation")
        width, height, min_width, min_height = self._window_bounds()
        self.compact_layout = width < 1280 or height < 760
        self._last_layout_compact = self.compact_layout
        self._initial_window_width = width
        self.geometry(f"{width}x{height}")
        self.minsize(min_width, min_height)
        self.config_model = load_config()
        self.client: ATClient | None = None
        self.transport_label = tk.StringVar(value="未连接")
        self.busy = False
        self.events: queue.Queue[tuple] = queue.Queue()
        self.frame_line_counts: dict[str, int] = {}
        self.ble_devices: list[BLEDeviceInfo] = []
        self.step_status_labels: dict[str, tk.Label] = {}
        self.step_status_state: dict[str, tuple[str, str]] = {}
        self._step_tree_values: dict[str, tuple[str, str]] = {}
        self._step_label_bbox: dict[str, tuple[int, int, int, int]] = {}
        self._step_status_layout_retries = 0
        self._step_tree_last_size: tuple[int, int] = (0, 0)
        self._step_tree_compact_columns: bool | None = None
        self._step_status_refresh_job: str | None = None
        self._step_status_configure_job: str | None = None
        self._window_resize_job: str | None = None
        self._pending_window_size: tuple[int, int] | None = None
        self._last_window_size: tuple[int, int] = (0, 0)
        self._resize_active = False
        self._step_status_refresh_deferred = False
        self._log_autoscroll_deferred = False
        self._help_panel_built = False
        self.active_flow_kind = ""
        self.active_flow_sn = ""
        self.last_half_sn = ""
        self.engineering_mode = False
        self.active_momo_prompt: tk.Toplevel | None = None
        self._log_line_count = 0
        self._ui_metrics = {
            "insert_calls": 0,
            "see_calls": 0,
            "ticks": 0,
            "control_events": 0,
            "log_events": 0,
        }
        self._build_vars()
        self._build_style()
        self._build_ui()
        self._refresh_ports()
        self._apply_access_state()
        self._center_window()
        self._restore_main_sash()
        self.bind("<Configure>", self._on_window_configure)
        self.after(80, self._poll_events)
        self._ensure_initial_credentials()

    def _ensure_initial_credentials(self) -> None:
        if has_engineer_password(self.config_model):
            return
        dialog = tk.Toplevel(self)
        dialog.title("首次设置")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        setup_ok = {"value": False}

        def cancel_setup() -> None:
            if dialog.winfo_exists():
                dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", cancel_setup)

        body = ttk.Frame(dialog, padding=20)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="本工位尚未配置工程密码。\n请先设置工程密码后才能进入主界面。\n工厂 token 可留空，稍后工程登录再设置。",
            justify=tk.LEFT,
            wraplength=420,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 14))

        password_var = tk.StringVar()
        confirm_var = tk.StringVar()
        token_var = tk.StringVar()
        show_secrets = tk.BooleanVar(value=False)

        ttk.Label(body, text="工程密码").grid(row=1, column=0, sticky=tk.W, pady=4)
        password_entry = ttk.Entry(body, textvariable=password_var, show="*", width=36)
        password_entry.grid(row=1, column=1, sticky=tk.EW, pady=4, padx=(10, 0))

        ttk.Label(body, text="确认密码").grid(row=2, column=0, sticky=tk.W, pady=4)
        confirm_entry = ttk.Entry(body, textvariable=confirm_var, show="*", width=36)
        confirm_entry.grid(row=2, column=1, sticky=tk.EW, pady=4, padx=(10, 0))

        ttk.Label(body, text="工厂 token（可选）").grid(row=3, column=0, sticky=tk.W, pady=4)
        token_entry = ttk.Entry(body, textvariable=token_var, show="*", width=36)
        token_entry.grid(row=3, column=1, sticky=tk.EW, pady=4, padx=(10, 0))

        def toggle_secret_visibility() -> None:
            mask = "" if show_secrets.get() else "*"
            password_entry.configure(show=mask)
            confirm_entry.configure(show=mask)
            token_entry.configure(show=mask)

        ttk.Checkbutton(
            body,
            text="显示输入内容",
            variable=show_secrets,
            command=toggle_secret_visibility,
            bootstyle="round-toggle",
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        error_var = tk.StringVar()
        ttk.Label(body, textvariable=error_var, foreground="#B91C1C", wraplength=420).grid(
            row=5, column=0, columnspan=2, sticky=tk.W, pady=(8, 0)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky=tk.E, pady=(16, 0))

        def save_setup() -> None:
            password = password_var.get().strip()
            confirm = confirm_var.get().strip()
            token = token_var.get().strip()
            if not password:
                error_var.set("请输入工程密码。")
                return
            if password != confirm:
                error_var.set("两次输入的工程密码不一致。")
                return
            try:
                save_engineer_password(password)
                if token:
                    save_factory_token(token)
            except Exception as exc:
                error_var.set(f"保存失败：{exc}")
                return
            setup_ok["value"] = True
            self.token_var.set("")
            self._sync_auth_status()
            self._log("OK", "首次工程密码已保存")
            if token:
                self._log("OK", "首次工厂 token 已保存")
            dialog.destroy()

        ttk.Button(buttons, text="取消并退出", bootstyle="secondary-outline", command=cancel_setup).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(buttons, text="保存并进入", bootstyle="primary", command=save_setup).pack(side=tk.LEFT)

        body.columnconfigure(1, weight=1)
        dialog.update_idletasks()
        x = max(0, self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        password_entry.focus_set()
        self.wait_window(dialog)
        if not setup_ok["value"]:
            self.destroy()


    def _window_bounds(self) -> tuple[int, int, int, int]:
        screen_width = max(800, self.winfo_screenwidth())
        screen_height = max(600, self.winfo_screenheight())
        reserve_width = 80 if screen_width >= 1100 else 40
        reserve_height = 80 if screen_height >= 760 else 60
        usable_width = max(720, screen_width - reserve_width)
        usable_height = max(520, screen_height - reserve_height)
        min_width = min(820, usable_width)
        min_height = min(560, usable_height)
        width = max(min_width, min(2360, usable_width, int(screen_width * 0.615)))
        height = max(min_height, min(1480, usable_height, int(screen_height * 0.685)))
        return width, height, min_width, min_height

    def _center_window(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 30)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _on_window_configure(self, event) -> None:
        if event.widget is not self:
            return
        size = (int(event.width), int(event.height))
        # Title-bar move fires Configure with unchanged size; ignore.
        if size == self._last_window_size and self._window_resize_job is None:
            return
        self._resize_active = True
        self._pending_window_size = size
        if self._window_resize_job is not None:
            try:
                self.after_cancel(self._window_resize_job)
            except tk.TclError:
                pass
        # Continuous resize: apply layout once after the drag settles.
        self._window_resize_job = self.after(UI_RESIZE_SETTLE_MS, self._on_window_resize_settled)

    def _on_window_resize_settled(self) -> None:
        self._window_resize_job = None
        size = self._pending_window_size
        if size is None:
            self._resize_active = False
            self._flush_resize_deferred_ui()
            return
        if size == self._last_window_size:
            self._resize_active = False
            self._flush_resize_deferred_ui()
            return
        self._last_window_size = size
        compact = size[0] < 1280 or size[1] < 760
        if compact != self._last_layout_compact:
            self.compact_layout = compact
            self._last_layout_compact = compact
            self._apply_responsive_layout()
        elif hasattr(self, "step_tree"):
            # Size changed but compact mode did not: only refresh status overlays once.
            self._schedule_step_status_refresh(0, force_relayout=True)
        self._resize_active = False
        self._flush_resize_deferred_ui()

    def _flush_resize_deferred_ui(self) -> None:
        if self._step_status_refresh_deferred and hasattr(self, "step_tree"):
            self._step_status_refresh_deferred = False
            self._schedule_step_status_refresh(0, force_relayout=True)
        if self._log_autoscroll_deferred and hasattr(self, "log_text"):
            self._log_autoscroll_deferred = False
            self._scroll_log_to_end()

    def _target_left_width(self, total_width: int | None = None) -> int:
        if total_width is None:
            total_width = max(self.winfo_width(), getattr(self, "_initial_window_width", 0), 1360)
        # Default left:right = 4:6
        target = int(total_width * 0.4)
        if self.compact_layout:
            return max(320, min(560, target))
        return max(520, min(960, target))

    def _apply_responsive_layout(self) -> None:
        left_width = self._target_left_width()
        if hasattr(self, "left_panel"):
            self.left_panel.configure(width=left_width)
        if hasattr(self, "main_panes"):
            self.after_idle(lambda width=left_width: self._set_main_sash(width))
        if hasattr(self, "connection_status_label"):
            self.connection_status_label.configure(
                width=10 if self.compact_layout else 12,
                padx=8,
            )
        if hasattr(self, "step_tree"):
            self.step_tree.configure(height=7 if self.compact_layout else 10)
            self._configure_step_tree_columns()
            self._schedule_step_status_refresh(0, force_relayout=True)
        if hasattr(self, "log_text"):
            self.log_text.configure(
                height=7 if self.compact_layout else 12,
                width=42 if self.compact_layout else 50,
            )

    def _configure_step_tree_columns(self) -> None:
        tree_width = self.step_tree.winfo_width()
        compact_columns = self.compact_layout or (tree_width > 1 and tree_width < 620)
        if compact_columns == self._step_tree_compact_columns:
            return
        self._step_tree_compact_columns = compact_columns

        if compact_columns:
            self.step_tree.configure(displaycolumns=("idx", "step", "status"))
            widths = {
                "idx": (44, 36, False),
                "step": (210, 150, True),
                "status": (92, 78, False),
                "detail": (0, 0, False),
            }
        else:
            self.step_tree.configure(displaycolumns=("idx", "step", "status", "detail"))
            widths = {
                "idx": (44, 36, False),
                "step": (300, 180, True),
                "status": (100, 82, False),
                "detail": (420, 160, True),
            }
        for column, (width, minwidth, stretch) in widths.items():
            self.step_tree.column(column, width=width, minwidth=minwidth, stretch=stretch)

    def _on_step_tree_configure(self) -> None:
        width = max(self.step_tree.winfo_width(), 1)
        height = max(self.step_tree.winfo_height(), 1)
        size = (width, height)
        if size == self._step_tree_last_size:
            return
        self._step_tree_last_size = size
        if self._step_status_configure_job is not None:
            try:
                self.after_cancel(self._step_status_configure_job)
            except tk.TclError:
                pass
        self._step_status_configure_job = self.after(UI_RESIZE_SETTLE_MS, self._on_step_tree_configure_settled)

    def _on_step_tree_configure_settled(self) -> None:
        self._step_status_configure_job = None
        self._configure_step_tree_columns()
        self._schedule_step_status_refresh(0, force_relayout=True)

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
        last_canvas_width = {"value": -1}
        last_scroll_region = {"value": None}
        pending_canvas_width: dict[str, int | None] = {"value": None}
        layout_job: dict[str, str | None] = {"value": None}

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def apply_scrollable_layout() -> None:
            layout_job["value"] = None
            width = pending_canvas_width["value"]
            pending_canvas_width["value"] = None
            if width is not None and width != last_canvas_width["value"]:
                last_canvas_width["value"] = width
                canvas.itemconfigure(window_id, width=width)
            region = canvas.bbox("all")
            if region == last_scroll_region["value"]:
                return
            last_scroll_region["value"] = region
            canvas.configure(scrollregion=region)

        def schedule_scrollable_layout() -> None:
            job = layout_job["value"]
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
            if self._resize_active:
                layout_job["value"] = self.after(UI_RESIZE_SETTLE_MS, apply_scrollable_layout)
            else:
                layout_job["value"] = self.after_idle(apply_scrollable_layout)

        def update_scroll_region(_event=None) -> None:
            schedule_scrollable_layout()

        def fit_content_width(event) -> None:
            width = int(event.width)
            if width == last_canvas_width["value"] and pending_canvas_width["value"] is None:
                return
            pending_canvas_width["value"] = width
            schedule_scrollable_layout()

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
        self.transport_var = tk.StringVar(value=cfg.prefer_transport or "UART")
        self.uart_port_var = tk.StringVar(value=cfg.uart_port)
        self.baud_var = tk.StringVar(value=str(cfg.uart_baudrate))
        self.ble_name_var = tk.StringVar(value=cfg.ble_name)
        self.ble_addr_var = tk.StringVar(value=cfg.ble_address_whitelist[0] if cfg.ble_address_whitelist else "")
        self.ble_scan_backend_var = tk.StringVar(value=cfg.ble_scan_backend or "nrf_dongle")
        self.ble_dongle_port_var = tk.StringVar(value=cfg.ble_dongle_port or "COM8")
        self.ble_dongle_sd_var = tk.StringVar(value=cfg.ble_dongle_sd_version or "auto")
        self.nrf_connect_ble_path_var = tk.StringVar(value=cfg.nrf_connect_ble_path)
        self.sn_enabled_var = tk.BooleanVar(value=cfg.sn_enabled)
        self.sn_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.role_var = tk.StringVar(value="操作员模式")
        self.auth_status_var = tk.StringVar()
        self.manual_hint_var = tk.StringVar()
        self.station_var = tk.StringVar(value=cfg.station_id)
        self.dut_alias_var = tk.StringVar(value=cfg.dut_alias)
        self.records_root_var = tk.StringVar(value=cfg.records_root)
        self.record_output_mode_var = tk.StringVar(value=_record_output_label(cfg.record_output_mode))
        self.ota_image_var = tk.StringVar(value=cfg.ota_image_path)
        self.firmware_repo_var = tk.StringVar(value=cfg.firmware_repo)
        self.flash_script_var = tk.StringVar(value=cfg.flash_script_path)
        self.half_flash_before_test_var = tk.BooleanVar(value=cfg.half_flash_before_test)
        self.flash_backend_var = tk.StringVar(value=cfg.flash_backend or "nrfjprog")
        self.flash_image_var = tk.StringVar(value=cfg.flash_image_path)
        self.half_flash_image_var = tk.StringVar(value=cfg.half_flash_image_path)
        self.flash_after_wait_var = tk.StringVar(value=str(cfg.flash_after_wait_s))
        self.flash_verify_var = tk.BooleanVar(value=cfg.flash_verify)
        self.nrfjprog_path_var = tk.StringVar(value=cfg.nrfjprog_path)
        self.half_flash_status_var = tk.StringVar()
        self.jlink_var = tk.StringVar(value=cfg.jlink_probe_id)
        self.sn_min_var = tk.StringVar(value=str(cfg.sn_rule.min_len))
        self.sn_max_var = tk.StringVar(value=str(cfg.sn_rule.max_len))
        self.sn_prefix_var = tk.StringVar(value=cfg.sn_rule.prefix)
        self.sn_regex_var = tk.StringVar(value=cfg.sn_rule.regex)

    def _build_style(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("LogTool.TButton", font=("Microsoft YaHei UI", 10), padding=(10, 5))
        style.configure("Toolbar.TButton", font=("Microsoft YaHei UI", 10), padding=(8, 4))
        style.configure(
            "Step.Treeview",
            font=("Microsoft YaHei UI", 11),
            rowheight=36,
            foreground="#111827",
        )
        style.configure(
            "Step.Treeview.Heading",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        action_font = ("Microsoft YaHei UI", 12, "bold")
        for bs in ("primary", "info", "success", "warning"):
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
        self._build_ble_tab(self.tabs)
        self._build_settings_tab(self.tabs)
        self._build_more_tab(self.tabs)

        self.right_monitor = ttk.Frame(right)
        self.right_monitor.pack(fill=tk.BOTH, expand=True)
        self._build_monitor(self.right_monitor)

        self.right_help = ttk.Frame(right)

        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _build_connection_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X)

        row0 = ttk.Frame(bar)
        row0.pack(fill=tk.X)
        ttk.Label(row0, text="通道").pack(side=tk.LEFT)
        ttk.Combobox(row0, textvariable=self.transport_var, values=("UART", "BLE"), width=7, state="readonly").pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row0, text="COM").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(row0, textvariable=self.uart_port_var, width=12)
        self.port_combo.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row0, text="波特率").pack(side=tk.LEFT)
        ttk.Entry(row0, textvariable=self.baud_var, width=10).pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row0, text="BLE 名").pack(side=tk.LEFT)
        ttk.Entry(row0, textvariable=self.ble_name_var, width=13).pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row0, text="地址").pack(side=tk.LEFT)
        ttk.Entry(row0, textvariable=self.ble_addr_var, width=24).pack(side=tk.LEFT, padx=(6, 0))

        row2 = ttk.Frame(bar)
        row2.pack(fill=tk.X, pady=(8, 0))
        tk.Button(
            row2,
            text="刷新",
            command=self._refresh_ports,
            width=8,
            font=("Microsoft YaHei UI", 10),
            bg="#EEF2F5",
            fg="#4B5563",
            relief=tk.FLAT,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            row2,
            text="连接",
            command=self._connect,
            width=14,
            font=("Microsoft YaHei UI", 11, "bold"),
            bg="#1ABC9C",
            fg="#FFFFFF",
            activebackground="#16A085",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            row2,
            text="断开",
            command=self._disconnect,
            width=8,
            font=("Microsoft YaHei UI", 10),
            bg="#95A5A6",
            fg="#FFFFFF",
            activebackground="#7F8C8D",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.connection_status_label = tk.Label(
            row2,
            textvariable=self.transport_label,
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

    def _build_run_tab(self, tabs: ttk.Notebook) -> None:
        _, frame = self._add_scrollable_tab(tabs, "工厂操作")

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
        ttk.Label(frame, text="权限").grid(row=3, column=0, sticky=tk.W)
        ttk.Label(frame, textvariable=self.role_var, style="Status.TLabel").grid(row=3, column=1, sticky=tk.W, pady=3)
        ttk.Label(frame, text="运行授权").grid(row=4, column=0, sticky=tk.W)
        ttk.Label(frame, textvariable=self.auth_status_var).grid(row=4, column=1, sticky=tk.W, pady=3)
        ttk.Label(frame, text="工位").grid(row=5, column=0, sticky=tk.W)
        ttk.Entry(frame, textvariable=self.station_var, width=28).grid(row=5, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="别名").grid(row=6, column=0, sticky=tk.W)
        ttk.Entry(frame, textvariable=self.dut_alias_var, width=28).grid(row=6, column=1, sticky=tk.EW, pady=3)

        ttk.Separator(frame).grid(row=7, column=0, columnspan=2, sticky=tk.EW, pady=12)
        self.half_btn = ttk.Button(frame, text="半机测试", bootstyle="info", command=lambda: self._run_flow("half"))
        self.half_btn.grid(row=8, column=0, columnspan=2, sticky=tk.EW, pady=5)
        self.full_btn = ttk.Button(frame, text="整机测试", bootstyle="success", command=lambda: self._run_flow("full"))
        self.full_btn.grid(row=9, column=0, columnspan=2, sticky=tk.EW, pady=5)
        ttk.Button(frame, text="OTA 升级", bootstyle="warning", command=self._run_ota).grid(row=10, column=0, columnspan=2, sticky=tk.EW, pady=5)

        ttk.Label(frame, textvariable=self.half_flash_status_var, wraplength=300, foreground="#6B7280").grid(
            row=11,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(4, 0),
        )

        ttk.Separator(frame).grid(row=12, column=0, columnspan=2, sticky=tk.EW, pady=12)
        ttk.Label(frame, text="工程调试", style="Title.TLabel").grid(row=13, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        self.manual_cmd_var = tk.StringVar(value="AT")
        self.manual_entry = ttk.Entry(frame, textvariable=self.manual_cmd_var)
        self.manual_entry.grid(row=14, column=0, columnspan=2, sticky=tk.EW, pady=3)
        self.manual_send_btn = ttk.Button(frame, text="发送 AT", bootstyle="primary", command=self._send_manual)
        self.manual_send_btn.grid(row=15, column=0, sticky=tk.EW, pady=3)
        self.probe_btn = ttk.Button(frame, text="探测 AT/VER", bootstyle="secondary", command=self._probe)
        self.probe_btn.grid(row=15, column=1, sticky=tk.EW, padx=(6, 0), pady=3)
        ttk.Label(frame, textvariable=self.manual_hint_var, wraplength=300, foreground="#6B7280").grid(
            row=16,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(4, 0),
        )
        frame.columnconfigure(1, weight=1)
        self._sync_sn_controls()

    def _build_flash_tab(self, tabs: ttk.Notebook) -> None:
        outer, frame = self._add_scrollable_tab(tabs, "芯片烧录")
        self.flash_tab = outer

        ttk.Label(frame, text="J-Link 烧录", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        ttk.Label(frame, text="权限").grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Label(frame, textvariable=self.role_var, style="Status.TLabel").grid(row=1, column=1, sticky=tk.W, pady=3)
        ttk.Label(frame, text="烧录方式").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.flash_backend_combo = ttk.Combobox(
            frame,
            textvariable=self.flash_backend_var,
            values=("nrfjprog", "script"),
            state="readonly",
            width=16,
        )
        self.flash_backend_combo.grid(row=2, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="固件 hex").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.flash_image_entry = ttk.Entry(frame, textvariable=self.flash_image_var, width=34)
        self.flash_image_entry.grid(row=3, column=1, sticky=tk.EW, pady=3)
        self.flash_image_browse_btn = ttk.Button(frame, text="...", width=3, bootstyle="light", command=self._browse_flash_image)
        self.flash_image_browse_btn.grid(row=3, column=2, padx=(5, 0), pady=3)
        ttk.Label(frame, text="nrfjprog").grid(row=4, column=0, sticky=tk.W, pady=3)
        self.nrfjprog_entry = ttk.Entry(frame, textvariable=self.nrfjprog_path_var, width=34)
        self.nrfjprog_entry.grid(row=4, column=1, sticky=tk.EW, pady=3)
        self.nrfjprog_browse_btn = ttk.Button(frame, text="...", width=3, bootstyle="light", command=self._browse_nrfjprog)
        self.nrfjprog_browse_btn.grid(row=4, column=2, padx=(5, 0), pady=3)
        ttk.Label(frame, text="J-Link ID").grid(row=5, column=0, sticky=tk.W, pady=3)
        self.flash_jlink_entry = ttk.Entry(frame, textvariable=self.jlink_var, width=34)
        self.flash_jlink_entry.grid(row=5, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="等待秒数").grid(row=6, column=0, sticky=tk.W, pady=3)
        self.flash_wait_entry = ttk.Entry(frame, textvariable=self.flash_after_wait_var, width=12)
        self.flash_wait_entry.grid(row=6, column=1, sticky=tk.W, pady=3)
        self.flash_verify_check = ttk.Checkbutton(
            frame,
            text="烧录后校验",
            variable=self.flash_verify_var,
            bootstyle="round-toggle",
        )
        self.flash_verify_check.grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(4, 8))

        self.flash_hash_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.flash_hash_var, wraplength=300, foreground="#6B7280").grid(
            row=8,
            column=0,
            columnspan=3,
            sticky=tk.W,
            pady=(0, 8),
        )

        self.flash_run_btn = ttk.Button(frame, text="开始烧录", bootstyle="danger", command=self._run_flash)
        self.flash_run_btn.grid(row=9, column=0, columnspan=3, sticky=tk.EW, pady=(8, 4))
        self.flash_probe_btn = ttk.Button(frame, text="烧录检测", bootstyle="secondary", command=self._run_flash_precheck)
        self.flash_probe_btn.grid(row=10, column=0, columnspan=3, sticky=tk.EW, pady=4)
        ttk.Label(
            frame,
            text="独立烧录属于工程操作，需要工程师登录。开始烧录不再自动预检；需要时点“烧录检测”。半机测试前自动烧录在“设置”中启用。",
            wraplength=300,
            foreground="#6B7280",
        ).grid(row=11, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        self._refresh_flash_text()

    def _build_ble_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs, padding=10)
        tabs.add(frame, text="BLE 扫描")
        ttk.Label(frame, text="自动扫描/人工选择", style="Title.TLabel").pack(anchor=tk.W)
        tools = ttk.Frame(frame)
        tools.pack(fill=tk.X, pady=(8, 8))
        ttk.Label(tools, text="后端").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(
            tools,
            textvariable=self.ble_scan_backend_var,
            values=("nrf_dongle", "windows", "auto"),
            width=11,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(tools, text="DONGLE").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(tools, textvariable=self.ble_dongle_port_var, width=8).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(tools, text="SD").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(
            tools,
            textvariable=self.ble_dongle_sd_var,
            values=("auto", "v5", "v2"),
            width=6,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(tools, text="扫描", bootstyle="info", command=self._scan_ble).pack(side=tk.LEFT)
        ttk.Button(tools, text="使用选中", bootstyle="light", command=self._use_selected_ble).pack(side=tk.LEFT, padx=(6, 0))
        self.ble_tree = ttk.Treeview(frame, columns=("name", "address", "rssi", "source"), show="headings", height=12)
        self.ble_tree.heading("name", text="Name")
        self.ble_tree.heading("address", text="Address")
        self.ble_tree.heading("rssi", text="RSSI")
        self.ble_tree.heading("source", text="Source")
        self.ble_tree.column("name", width=120)
        self.ble_tree.column("address", width=170)
        self.ble_tree.column("rssi", width=55, anchor=tk.CENTER)
        self.ble_tree.column("source", width=120)
        self.ble_tree.pack(fill=tk.BOTH, expand=True)

    def _build_settings_tab(self, tabs: ttk.Notebook) -> None:
        outer, frame = self._add_scrollable_tab(tabs, "设置")
        self.settings_tab = outer
        self.settings_engineering_controls = []
        rows = [
            ("固件仓库", self.firmware_repo_var, self._browse_repo, "firmware_repo"),
            ("烧录脚本", self.flash_script_var, self._browse_flash_script, "flash_script"),
            ("独立烧录固件", self.flash_image_var, self._browse_flash_image, "flash_image"),
            ("半机烧录固件", self.half_flash_image_var, self._browse_half_flash_image, "half_flash_image"),
            ("nrfjprog", self.nrfjprog_path_var, self._browse_nrfjprog, "nrfjprog"),
            ("J-Link", self.jlink_var, None, "flash_jlink"),
            ("烧录等待(s)", self.flash_after_wait_var, None, "flash_wait"),
            ("记录目录", self.records_root_var, self._browse_records, None),
            ("OTA 包", self.ota_image_var, self._browse_ota_image, None),
            ("SN 最小", self.sn_min_var, None, None),
            ("SN 最大", self.sn_max_var, None, None),
            ("SN 前缀", self.sn_prefix_var, None, None),
            ("SN 正则", self.sn_regex_var, None, None),
        ]
        for row, (label, var, browse, control_name) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
            entry = ttk.Entry(frame, textvariable=var, width=34)
            entry.grid(row=row, column=1, sticky=tk.EW, pady=3)
            if control_name:
                setattr(self, f"{control_name}_entry", entry)
                self.settings_engineering_controls.append(entry)
            if browse:
                button = ttk.Button(frame, text="...", width=3, bootstyle="light", command=browse)
                button.grid(row=row, column=2, padx=(5, 0), pady=3)
                if control_name:
                    setattr(self, f"{control_name}_browse_btn", button)
                    self.settings_engineering_controls.append(button)
        flash_backend_row = len(rows)
        ttk.Label(frame, text="烧录方式").grid(row=flash_backend_row, column=0, sticky=tk.W, pady=3)
        self.settings_flash_backend_combo = ttk.Combobox(
            frame,
            textvariable=self.flash_backend_var,
            values=("nrfjprog", "script"),
            state="readonly",
            width=34,
        )
        self.settings_flash_backend_combo.grid(row=flash_backend_row, column=1, sticky=tk.EW, pady=3)
        self.settings_engineering_controls.append(self.settings_flash_backend_combo)
        flash_option_row = flash_backend_row + 1
        self.half_flash_before_test_check = ttk.Checkbutton(
            frame,
            text="半机测试前烧录",
            variable=self.half_flash_before_test_var,
            bootstyle="round-toggle",
            command=self._refresh_flash_text,
        )
        self.half_flash_before_test_check.grid(row=flash_option_row, column=0, columnspan=2, sticky=tk.W, pady=(4, 2))
        self.settings_engineering_controls.append(self.half_flash_before_test_check)
        self.settings_flash_verify_check = ttk.Checkbutton(
            frame,
            text="烧录后校验",
            variable=self.flash_verify_var,
            bootstyle="round-toggle",
        )
        self.settings_flash_verify_check.grid(row=flash_option_row + 1, column=0, columnspan=2, sticky=tk.W, pady=(2, 8))
        self.settings_engineering_controls.append(self.settings_flash_verify_check)
        record_row = flash_option_row + 2
        ttk.Label(frame, text="记录格式").grid(row=record_row, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(
            frame,
            textvariable=self.record_output_mode_var,
            values=tuple(RECORD_OUTPUT_MODE_LABELS.values()),
            state="readonly",
            width=34,
        ).grid(row=record_row, column=1, sticky=tk.EW, pady=3)
        ttk.Button(frame, text="保存设置", bootstyle="success", command=self._save_settings).grid(row=record_row + 1, column=0, columnspan=3, sticky=tk.EW, pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def _build_more_tab(self, tabs: ttk.Notebook) -> None:
        _, frame = self._add_scrollable_tab(tabs, "更多")

        ttk.Label(frame, text="工程权限", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        ttk.Label(frame, text="当前权限").grid(row=1, column=0, sticky=tk.W)
        ttk.Label(frame, textvariable=self.role_var, style="Status.TLabel").grid(row=1, column=1, sticky=tk.W, pady=3)
        ttk.Label(frame, text="运行授权").grid(row=2, column=0, sticky=tk.W)
        ttk.Label(frame, textvariable=self.auth_status_var).grid(row=2, column=1, sticky=tk.W, pady=3)
        ttk.Label(frame, text="运行 token").grid(row=3, column=0, sticky=tk.W)
        self.token_state_label = ttk.Label(frame, textvariable=self.auth_status_var)
        self.token_state_label.grid(row=3, column=1, sticky=tk.W, pady=3)

        login_row = ttk.Frame(frame)
        login_row.grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(6, 12))
        self.engineer_login_btn = ttk.Button(login_row, text="工程登录", bootstyle="secondary", command=self._login_engineering)
        self.engineer_login_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.engineer_logout_btn = ttk.Button(login_row, text="退出工程", bootstyle="light", command=self._logout_engineering)
        self.engineer_logout_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        self.set_token_btn = ttk.Button(frame, text="设置/更新运行 token", bootstyle="primary", command=self._set_factory_token)
        self.set_token_btn.grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(0, 12))

        ttk.Separator(frame).grid(row=6, column=0, columnspan=2, sticky=tk.EW, pady=12)
        ttk.Label(frame, text="帮助", style="Title.TLabel").grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        ttk.Label(
            frame,
            text="切换到“更多”时，右侧会显示完整帮助内容。",
            wraplength=300,
            foreground="#6B7280",
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W)
        frame.columnconfigure(1, weight=1)

    def _on_tab_changed(self, _e=None) -> None:
        current = self.tabs.select()
        text = self.tabs.tab(current, "text")
        if text == "更多":
            self.right_monitor.pack_forget()
            if not self._help_panel_built:
                self._build_help_panel(self.right_help)
                self._help_panel_built = True
            self.right_help.pack(fill=tk.BOTH, expand=True)
        else:
            self.right_help.pack_forget()
            self.right_monitor.pack(fill=tk.BOTH, expand=True)

    def _build_help_panel(self, parent: ttk.Frame) -> None:
        inner = ttk.Notebook(parent)
        inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        usage_frame = ttk.Frame(inner, padding=10)
        inner.add(usage_frame, text="使用说明")
        usage_text = tk.Text(usage_frame, wrap=tk.WORD, font=("Microsoft YaHei UI", 10), state=tk.DISABLED)
        usage_scroll = ttk.Scrollbar(usage_frame, orient=tk.VERTICAL, command=usage_text.yview)
        usage_text.configure(yscrollcommand=usage_scroll.set)
        usage_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        usage_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        usage_content = """POC3A Factory Workstation 使用说明
================================

1. 连接设备
   - UART：选择 COM 口和波特率（默认 460800），点击"连接"
   - BLE：输入 BLE 广播名（默认 AXI-P1-T）和地址，点击"连接"
   - 连接成功后状态栏显示"已连接"

2. 工厂测试
   - 正式产测：勾选"启用 SN/记录"，填写 SN、工位、别名
   - 临时联调：取消勾选"启用 SN/记录"，允许空 SN，测试不写 CSV/日志文件
   - 点击"半机测试"或"整机测试"执行对应测试流程
   - 运行授权从 .env/环境变量读取；工程登录只用于初始化/更新 token
   - 测试结果会显示在右侧"执行步骤"和"AT 日志"中

3. 工程调试
   - 操作员模式不开放工程调试
   - 手动 AT 仅工程登录后可用，使用完请退出工程模式
   - 点击"探测 AT/VER"快速发送 AT 和 AT+VER?

4. OTA 升级
   - 在"设置"中选择 OTA 包路径
   - 点击"OTA 升级"按钮
   - 确认弹窗后开始升级

5. 设置保存
   - 在"设置"tab 中修改配置
   - 点击"保存设置"保存到 config.json

6. 记录文件
   - 测试记录默认保存在 factory_records/ 目录
   - 每次测试会生成独立的 CSV 和日志文件
"""
        usage_text.configure(state=tk.NORMAL)
        usage_text.insert(tk.END, usage_content)
        usage_text.configure(state=tk.DISABLED)

        at_frame = ttk.Frame(inner, padding=10)
        inner.add(at_frame, text="AT 指令")
        at_text = tk.Text(at_frame, wrap=tk.WORD, font=("Consolas", 10), state=tk.DISABLED)
        at_scroll = ttk.Scrollbar(at_frame, orient=tk.VERTICAL, command=at_text.yview)
        at_text.configure(yscrollcommand=at_scroll.set)
        at_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        at_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        at_content = """AT 指令参考
==========

基础指令
------
AT                - 连通性测试
AT+HELP?          - 列出命令分组
AT+VER?           - 固件版本
AT+STATUS?        - 设备状态
AT+CAP?           - 当前能力位
AT+ERR?           - 最近错误
AT+RST            - 重启设备
AT+UARTLOG?       - 查询 UART 诊断输出门控
AT+UARTLOG=<0|1>  - 控制 UART 诊断输出

配置指令
------
AT+TIME=<utc_sec>,<tz_min>                    - 同步 UTC 时间
AT+TIME?                                      - 查询 UTC 时间
AT+CFG=<start_sec>,<duration_sec>,<tz_min>,<ritual_min>,<intensity>  - 写核心配置
AT+CFG?                                       - 查询配置
AT+PAIRCLR                                    - 清除配对标志

状态机指令
------
AT+PREMON     - 请求进入预监测
AT+STOP       - 强制停止
AT+SM?        - 查询状态机
AT+SESSION?   - 查询当前会话
AT+SKIP?      - 查询当前计划窗跳过标记

模拟输入指令（需 CONFIG_POC3A_AT_TEST=y）
------
AT+SIMWEAR=<0|1>              - 模拟佩戴状态
AT+SIMBAT=<percent>,<charging> - 模拟电量和充电
AT+SIMINT=<duration_min>      - 模拟算法干预请求
AT+SIMTAP=DOUBLE              - 模拟双击

报告与上传
------
AT+MOCKREPORT     - 生成 mock report
AT+REPORT?        - 查询报告摘要
AT+FEATURE=<report_id>,<offset>  - 请求特征分片
AT+INTERLOG?      - 查询干预日志
AT+UPLOADSTAT?    - 最近上传状态

外设产测指令（需 CONFIG_POC3A_AT_HW_TEST=y）
------
AT+FACTORY?                       - 查询工厂模式
AT+FACTORY=UNLOCK,<token>         - 解锁工厂模式
AT+FACTORY=LOCK                   - 锁回工厂模式
AT+FACTORY=EXIT                   - 结束并锁回

AT+HW?                            - 查询 HW 测试能力
AT+HW=LIST,BARE|HALF|FULL         - 列出某级别测试项
AT+HW=RUN[,LEVEL][,CONFIRM]       - 运行某级别所有测试

单项测试：
AT+HW=POWER                       - 电源轨测试
AT+HW=IMU,PROBE                   - IMU 探测
AT+HW=IMU,STREAM,CONFIRM          - IMU 连续采样
AT+HW=IMU,VIBFEEDBACK,CONFIRM,<amp_pct>,<hold_ms>  - IMU 振动反馈检测
AT+HW=TOUCH,PROBE                 - 触摸探测
AT+HW=CHG,PROBE                   - 充电 IC 探测
AT+HW=CHG,REGS                    - 充电 IC 寄存器读回
AT+HW=CHG,CONFIGURE,CONFIRM       - 充电 IC 配置
AT+HW=CHG,ISR,CONFIRM             - 充电 IC 中断（需 USB 插拔）
AT+HW=GAUGE,PROBE                 - 电量计探测
AT+HW=GAUGE,DATA                  - 电量计数据读取
AT+HW=FLASH,PROBE                 - Flash 探测
AT+HW=FLASH,RWE,CONFIRM           - Flash 读写擦
AT+HW=FLASH,STRESS,CONFIRM        - Flash 压力测试
AT+HW=FLASH,LFS,CONFIRM           - Flash LittleFS 测试
AT+HW=PPG,PROBE                   - PPG 探测
AT+HW=PPG,READREG                 - PPG 寄存器读回
AT+HW=PPG,FIFO,CONFIRM            - PPG FIFO 测试
AT+HW=PPG,SAMPLE,CONFIRM          - PPG 采样测试
AT+HW=PPG,ISR,CONFIRM             - PPG 中断测试
AT+HW=HAPTIC,READY                - 触觉就绪检查
AT+HW=HAPTIC,SMOKE,CONFIRM        - 触觉 smoke 测试
AT+HW=HAPTIC,LRA,CONFIRM          - LRA 测试
AT+HW=HAPTIC,BREATHING,CONFIRM    - 呼吸包络测试

触觉 / 电池 / 电源
------
AT+HAPTIC=<CONSTANT|PULSE|BREATHING>,<intensity>,<duration_ms>  - 触觉预览
AT+HAPTIC=STOP                    - 停止触觉
AT+BAT?                           - 查询电量
AT+PWR?                           - 查询充电/电源快照
AT+PWR=<key>,<value>              - 设置充电参数（ILIM/ICHG/VSYS/VTERM/WATCHDOG/HIZ/CHGEN/FEEDDOG）

OTA
------
AT+OTA?                           - OTA 状态
AT+OTABUSY?                       - OTA 业务锁

诊断
------
AT+DIAG=STACK                     - 栈高水位诊断（需编译开启）

通用前提
------
- CONFIG_POC3A_AT_TEST=y 是 AT 指令总开关
- CONFIG_POC3A_AT_HW_TEST=y 是产测指令开关
- 工厂模式执行项需先 AT+FACTORY=UNLOCK 解锁
- OTA 进行中只允许只读/诊断指令
- 操作员模式不开放工程调试；手动 AT 仅工程登录后可用
"""
        at_text.configure(state=tk.NORMAL)
        at_text.insert(tk.END, at_content)
        at_text.configure(state=tk.DISABLED)

    def _build_monitor(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=3)
        parent.rowconfigure(3, weight=2)

        ttk.Label(parent, text="执行步骤", style="Title.TLabel").grid(row=0, column=0, sticky=tk.W)
        step_frame = ttk.Frame(parent)
        step_frame.grid(row=1, column=0, sticky=tk.NSEW, pady=(6, 5))
        self.step_tree = ttk.Treeview(
            step_frame,
            columns=("idx", "step", "status", "detail"),
            show="headings",
            style="Step.Treeview",
            height=7 if self.compact_layout else 10,
        )
        for col, title in (
            ("idx", "#"),
            ("step", "步骤"),
            ("status", "状态"),
            ("detail", "详情"),
        ):
            self.step_tree.heading(col, text=title)
        self._configure_step_tree_columns()
        self.step_tree.bind("<Configure>", lambda _e: self._on_step_tree_configure())
        self.step_tree.bind(
            "<ButtonRelease-1>",
            lambda _e: self._schedule_step_status_refresh(UI_STEP_STATUS_REFRESH_MS),
        )
        self.step_tree.bind(
            "<KeyRelease>",
            lambda _e: self._schedule_step_status_refresh(UI_STEP_STATUS_REFRESH_MS),
        )
        self.step_tree.pack(fill=tk.BOTH, expand=True)

        log_header = ttk.Frame(parent)
        log_header.grid(row=2, column=0, sticky=tk.EW, pady=(8, 0))
        ttk.Label(log_header, text="AT 日志", style="Title.TLabel").pack(side=tk.LEFT, anchor=tk.W)
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
            height=7 if self.compact_layout else 12,
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

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        values = [port.device for port in ports]
        self.port_combo.configure(values=values)
        if not self.uart_port_var.get() and values:
            self.uart_port_var.set(values[0])
        if not ports:
            self._log("WARN", "未枚举到串口；确认 pyserial 已安装且 DUT 已连接")

    def _connect(self) -> None:
        if self.busy:
            return
        if self.client is not None and self._client_alive():
            self._log("INFO", "已经连接")
            return
        if self.client is not None:
            self._close_dead_client()
        mode = self.transport_var.get().upper() or "UART"
        self._set_connection_status("CONNECTING", f"{mode} 连接中...")
        self._set_busy(True)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _make_client_from_current_settings(self, line_callback) -> tuple[ATClient, str, str]:
        mode = self.transport_var.get().upper()
        if mode == "BLE":
            ble_name = self.ble_name_var.get().strip() or "AXI-P1-T"
            ble_addr = self.ble_addr_var.get().strip()
            backend = self.ble_scan_backend_var.get().strip() or "nrf_dongle"
            transport = BLENusTransport(
                ble_name,
                ble_addr,
                backend=backend,
                dongle_port=self.ble_dongle_port_var.get().strip() or "COM8",
                nrf_connect_ble_path=self.nrf_connect_ble_path_var.get().strip(),
                dongle_sd_version=self.ble_dongle_sd_var.get().strip() or "auto",
            )
            label = f"{ble_name} {ble_addr}".strip()
        else:
            mode = "UART"
            port = self.uart_port_var.get().strip()
            if not port:
                raise RuntimeError("UART port is empty")
            baudrate = int(self.baud_var.get().strip() or "115200")
            transport = UARTTransport(port, baudrate)
            label = f"{port}@{baudrate}"
        return ATClient(transport, line_callback), mode, label

    def _connect_worker(self) -> None:
        try:
            client, mode, label = self._make_client_from_current_settings(self._line_callback)
            self.events.put(("connected", client, mode, label))
        except Exception as exc:
            self.events.put(("log", "ERR", f"连接失败: {exc}"))
            self.events.put(("connection_status", "DISCONNECTED", "未连接"))
        finally:
            self.events.put(("busy", False))

    def _disconnect(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception as exc:
                self._log("WARN", f"断开时异常: {exc}")
        self.client = None
        self._set_connection_status("DISCONNECTED", "未连接")
        self._log("INFO", "已断开")

    def _client_alive(self) -> bool:
        if self.client is None:
            return False
        try:
            return bool(self.client.is_connected())
        except Exception:
            return False

    def _close_dead_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self._set_connection_status("DISCONNECTED", "未连接")

    def _set_connection_status(self, state: str, detail: str) -> None:
        state_key = state.upper()
        if state_key == "UART":
            text = "UART 已连接"
            bg = "#008F3A"
        elif state_key == "BLE":
            text = "BLE 已连接"
            bg = "#006DFF"
        elif state_key == "CONNECTING":
            text = detail or "连接中..."
            bg = "#C97800"
        else:
            text = detail or "未连接"
            bg = "#6B7280"
        self.transport_label.set(text)
        if hasattr(self, "connection_status_label"):
            self.connection_status_label.configure(bg=bg, fg="#FFFFFF")

    def _probe(self) -> None:
        self._run_commands([("AT probe", "AT"), ("Read version", "AT+VER?")])

    def _half_flash_config(self) -> WorkstationConfig:
        return replace(self.config_model, flash_image_path=self.config_model.half_flash_image_path)

    def _validate_flash_ready(self, config: WorkstationConfig | None = None, *, half_flow: bool = False) -> bool:
        """Fast local checks only. Full J-Link/nrfjprog probe is manual via 烧录检测."""
        cfg = config or self.config_model
        image = str(cfg.flash_image_path or "").strip()
        backend = str(cfg.flash_backend or "nrfjprog").strip() or "nrfjprog"
        image_label = "半机烧录固件" if half_flow else "烧录固件"
        if backend == "nrfjprog":
            if not image:
                messagebox.showerror(f"缺少{image_label}", f"请先在设置中选择{image_label} merged.hex。")
                return False
            if not Path(image).exists():
                messagebox.showerror(f"{image_label}不存在", f"找不到{image_label}：\n{image}")
                return False
        elif backend == "script":
            script = str(cfg.flash_script_path or "").strip()
            if not script or not Path(script).exists():
                messagebox.showerror("烧录脚本不存在", f"找不到烧录脚本：\n{script}")
                return False
        else:
            messagebox.showerror("烧录方式无效", f"不支持的烧录方式：{backend}")
            return False
        return True

    def _flash_payload(self, outcome: FlashOutcome | None = None) -> dict:
        return flash_payload(self.config_model, outcome)

    def _run_flash(self) -> None:
        if self.busy:
            return
        if not self.engineering_mode:
            messagebox.showwarning("需要工程登录", "芯片烧录属于工程操作，请先在“更多”中工程登录。")
            return
        self._save_settings(silent=True)
        if not self._validate_flash_ready():
            return
        image_name = Path(self.config_model.flash_image_path).name if self.config_model.flash_image_path else "未选择"
        if not messagebox.askyesno("确认芯片烧录", f"即将烧录固件：{image_name}\n\n烧录会复位设备，是否继续？"):
            return
        self._close_client_for_flash()
        self._set_busy(True)

        def worker() -> None:
            try:
                def flash_log(direction: str, line: str) -> None:
                    self.events.put(("log", direction, line))

                outcome = run_flash(self.config_model, flash_log)
                level = "OK" if outcome.ok else "ERR"
                self.events.put(("log", level, f"芯片烧录 {outcome.result}: {outcome.message} ({outcome.elapsed_ms} ms)"))
                if outcome.ok and self.config_model.flash_after_wait_s > 0:
                    time.sleep(max(0.0, float(self.config_model.flash_after_wait_s)))
            except Exception as exc:
                self.events.put(("log", "ERR", f"芯片烧录失败: {exc}"))
            finally:
                self.events.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def _run_flash_precheck(self) -> None:
        if self.busy:
            return
        if not self.engineering_mode:
            messagebox.showwarning("需要工程登录", "烧录检测属于工程操作，请先在“更多”中工程登录。")
            return
        self._save_settings(silent=True)
        self._set_busy(True)

        def worker() -> None:
            try:
                precheck = precheck_flash_request(
                    self.config_model,
                    sn_enabled=bool(self.sn_enabled_var.get()),
                    dry_run=not bool(self.sn_enabled_var.get()),
                )
                detail = precheck.message
                if precheck.probe_ids:
                    detail = f"{detail}\n探针: {', '.join(precheck.probe_ids)}"
                if precheck.ok and precheck.level != "WARN":
                    self.events.put(("log", "OK", f"烧录检测通过: {precheck.message}"))
                    self.events.put(("popup", "info", "烧录检测通过", detail))
                elif precheck.ok:
                    self.events.put(("log", "WARN", f"烧录检测警告: {precheck.message}"))
                    self.events.put(("popup", "warning", "烧录检测警告", detail))
                else:
                    self.events.put(("log", "ERR", f"烧录检测失败: {precheck.message}"))
                    self.events.put(("popup", "error", "烧录检测失败", detail))
            except Exception as exc:
                self.events.put(("log", "ERR", f"烧录检测失败: {exc}"))
                self.events.put(("popup", "error", "烧录检测失败", str(exc)))
            finally:
                self.events.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def _close_client_for_flash(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                self.events.put(("log", "WARN", f"烧录前关闭连接失败: {exc}"))
        self.events.put(("connection_status", "DISCONNECTED", "烧录期间断开"))

    def _run_flash_for_flow(self, record, progress) -> FlashOutcome:
        def flash_log(direction: str, line: str) -> None:
            self.events.put(("log", direction, line))

        return record_flash_step(
            self._half_flash_config(),
            record,
            progress,
            run_flash,
            step_index=1,
            line_callback=flash_log,
        )

    def _reconnect_after_flash(self, record, line_cb, progress) -> ATClient | None:
        label = "Flash reconnect"
        progress(2, label, "RUN", self.transport_var.get().upper() or "UART")
        started = time.monotonic()
        try:
            client, mode, detail = self._make_client_from_current_settings(line_cb)
            self.client = client
            self.events.put(("connected", client, mode, detail))
            ok, elapsed_ms, summary, reason, _results = probe_at_client(client)
            record.log_item(
                "half",
                label,
                "AT;AT+VER?",
                "PASS" if ok else "NG",
                elapsed_ms,
                reason,
                summary,
            )
            progress(2, label, "PASS" if ok else "NG", summary or f"{elapsed_ms / 1000:.1f}s")
            return client if ok else None
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            record.log_item("half", label, "connect", "NG", elapsed_ms, str(exc), "")
            progress(2, label, "NG", str(exc))
            return None

    def _runtime_token_for_flow(self) -> str:
        return get_factory_token("")

    def _sync_auth_status(self) -> None:
        has_token = bool(self._runtime_token_for_flow())
        self.auth_status_var.set("已配置" if has_token else "未配置")

    def _refresh_flash_text(self) -> None:
        image_text = self.flash_image_var.get().strip()
        half_image_text = self.half_flash_image_var.get().strip()
        image_name = Path(image_text).name if image_text else "未选择"
        half_image_name = Path(half_image_text).name if half_image_text else "未选择"
        if self.half_flash_before_test_var.get():
            self.half_flash_status_var.set(f"半机测试前烧录：开启，固件 {half_image_name}")
        else:
            self.half_flash_status_var.set("半机测试前烧录：关闭")
        hash_text = ""
        if image_text and Path(image_text).exists():
            try:
                digest = file_sha256(image_text)
                size = Path(image_text).stat().st_size
                hash_text = f"固件：{image_name} | size={size} | sha256={digest[:12]}..."
            except Exception as exc:
                hash_text = f"固件：{image_name} | hash 读取失败：{exc}"
        elif image_text:
            hash_text = f"固件不存在：{image_text}"
        else:
            hash_text = "尚未选择烧录固件"
        if hasattr(self, "flash_hash_var"):
            self.flash_hash_var.set(hash_text)

    def _apply_access_state(self) -> None:
        self.role_var.set("工程模式" if self.engineering_mode else "操作员模式")
        if hasattr(self, "engineer_login_btn"):
            self.engineer_login_btn.configure(state=tk.DISABLED if self.engineering_mode else tk.NORMAL)
        if hasattr(self, "engineer_logout_btn"):
            self.engineer_logout_btn.configure(state=tk.NORMAL if self.engineering_mode else tk.DISABLED)
        if hasattr(self, "set_token_btn"):
            self.set_token_btn.configure(state=tk.NORMAL if self.engineering_mode else tk.DISABLED)
        debug_state = tk.NORMAL if self.engineering_mode else tk.DISABLED
        if hasattr(self, "manual_entry"):
            self.manual_entry.configure(state=debug_state)
        if hasattr(self, "manual_send_btn"):
            self.manual_send_btn.configure(state=debug_state)
        if hasattr(self, "probe_btn"):
            self.probe_btn.configure(state=debug_state)
        if hasattr(self, "tabs") and hasattr(self, "settings_tab"):
            self.tabs.tab(self.settings_tab, state=tk.NORMAL)
        flash_state = tk.NORMAL if self.engineering_mode and not self.busy else tk.DISABLED
        for name in (
            "flash_run_btn",
            "flash_probe_btn",
            "flash_backend_combo",
            "flash_image_entry",
            "flash_image_browse_btn",
            "nrfjprog_entry",
            "nrfjprog_browse_btn",
            "flash_jlink_entry",
            "flash_wait_entry",
            "flash_verify_check",
        ):
            if hasattr(self, name):
                control = getattr(self, name)
                enabled_state = "readonly" if isinstance(control, ttk.Combobox) else tk.NORMAL
                control.configure(state=enabled_state if flash_state == tk.NORMAL else tk.DISABLED)
        for control in getattr(self, "settings_engineering_controls", []):
            enabled_state = "readonly" if isinstance(control, ttk.Combobox) else tk.NORMAL
            control.configure(state=enabled_state if flash_state == tk.NORMAL else tk.DISABLED)
        self.manual_hint_var.set(
            "工程模式：允许手动发送 AT 指令，危险操作会写入 AT 日志。"
            if self.engineering_mode
            else "操作员模式：工程调试不可用；测试流程会自动使用隐藏运行授权。"
        )
        self._sync_auth_status()

    def _ask_secret(
        self,
        title: str,
        prompt: str,
        *,
        allow_empty: bool = False,
    ) -> str | None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        result: dict[str, str | None] = {"value": None}

        def close_dialog() -> None:
            if dialog.winfo_exists():
                dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        body = ttk.Frame(dialog, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=prompt, justify=tk.LEFT, wraplength=360).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10)
        )

        value_var = tk.StringVar()
        show_secret = tk.BooleanVar(value=False)
        entry = ttk.Entry(body, textvariable=value_var, show="*", width=36)
        entry.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=4)

        def toggle_visibility() -> None:
            entry.configure(show="" if show_secret.get() else "*")

        ttk.Checkbutton(
            body,
            text="显示输入内容",
            variable=show_secret,
            command=toggle_visibility,
            bootstyle="round-toggle",
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        error_var = tk.StringVar()
        ttk.Label(body, textvariable=error_var, foreground="#B91C1C").grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=(6, 0)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky=tk.E, pady=(14, 0))

        def confirm() -> None:
            value = value_var.get().strip()
            if not value and not allow_empty:
                error_var.set("请输入内容。")
                return
            result["value"] = value
            close_dialog()

        ttk.Button(buttons, text="取消", bootstyle="secondary-outline", command=close_dialog).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(buttons, text="确定", bootstyle="primary", command=confirm).pack(side=tk.LEFT)

        body.columnconfigure(0, weight=1)
        dialog.update_idletasks()
        x = max(0, self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        entry.focus_set()
        self.wait_window(dialog)
        return result["value"]

    def _login_engineering(self) -> None:
        password = self._ask_secret("工程登录", "请输入工程密码：")
        if password is None:
            return
        if not verify_engineer_password(password, self.config_model):
            messagebox.showerror(
                "工程登录失败",
                "工程密码不正确。",
            )
            self._log("WARN", "工程登录失败")
            return
        self.engineering_mode = True
        self._apply_access_state()
        self._log("OK", "已进入工程模式")

    def _set_factory_token(self) -> None:
        if not self.engineering_mode:
            messagebox.showwarning("需要工程登录", "请先在“更多”中完成工程登录。")
            return
        token = self._ask_secret("设置运行 token", "请输入设备工厂 token：")
        if token is None:
            return
        try:
            save_factory_token(token)
        except Exception as exc:
            messagebox.showerror("保存 token 失败", f"运行 token 未保存。\n\n{exc}")
            self._log("ERR", f"保存 token 失败: {exc}")
            return
        self.token_var.set("")
        self._sync_auth_status()
        messagebox.showinfo("运行 token 已保存", "运行 token 已保存到本工位隐藏配置，操作员无需登录即可测试。")
        self._log("OK", "运行 token 已保存")

    def _logout_engineering(self) -> None:
        self.engineering_mode = False
        self.token_var.set("")
        self._apply_access_state()
        if hasattr(self, "tabs") and hasattr(self, "settings_tab") and self.tabs.select() == str(self.settings_tab):
            self.tabs.select(0)
        self._log("INFO", "已退出工程模式")

    def _send_manual(self) -> None:
        command = self.manual_cmd_var.get().strip()
        if not command:
            return
        if not self.engineering_mode:
            messagebox.showwarning(
                "需要工程权限",
                "操作员模式不开放工程调试。\n\n"
                "请由工程人员登录后再发送手动 AT。",
            )
            self._log("WARN", f"操作员模式拦截手动 AT: {redact_sensitive_text(command)}")
            return
        self._run_commands([("Manual", command)])

    def _sync_sn_controls(self) -> None:
        if hasattr(self, "sn_entry"):
            self.sn_entry.configure(state=tk.NORMAL if self.sn_enabled_var.get() else tk.DISABLED)

    def _run_commands(self, steps: list[tuple[str, str]]) -> None:
        if not self._ensure_client() or self.busy:
            return
        self._clear_steps()
        self.frame_line_counts = {}
        self._set_busy(True)

        def worker() -> None:
            try:
                for idx, (label, command) in enumerate(steps, start=1):
                    self.events.put(("step", idx, label, "RUN", redact_sensitive_text(command)))
                    result = self.client.send_command(command, self.config_model.at_timeouts.for_command(command))  # type: ignore[union-attr]
                    detail = " ; ".join(result.lines[-3:]) if result.lines else f"{result.elapsed_s:.1f}s"
                    self.events.put(("step", idx, label, result.status_text, detail))
            except Exception as exc:
                self.events.put(("log", "ERR", f"命令执行失败: {exc}"))
            finally:
                self.events.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def _run_flow(self, kind: str) -> None:
        if self.busy:
            return
        self._save_settings(silent=True)
        auto_flash = kind == "half" and self.config_model.half_flash_before_test
        if auto_flash and not self._validate_flash_ready(self._half_flash_config(), half_flow=True):
            return
        if not auto_flash and not self._ensure_client():
            return
        sn_enabled = self.sn_enabled_var.get()
        sn = self.sn_var.get().strip() if sn_enabled else ""
        token = self._runtime_token_for_flow()
        self._sync_auth_status()
        if sn_enabled:
            if not sn:
                messagebox.showerror(
                    "SN 不能为空",
                    "当前已启用 SN/记录，请先扫码输入 SN。\n\n临时联调请取消勾选“启用 SN/记录”。",
                )
                return
            ok, reason = self.config_model.validate_sn(sn)
            if not ok:
                messagebox.showerror("SN 不符合规则", f"SN 不符合规则：{self._format_sn_error(reason)}\n\n请重新扫码。")
                return
            if self.config_model.factory_at_required and not token:
                messagebox.showerror(
                    "缺少运行 token",
                    "正式产测需要运行授权才能解锁工厂模式。\n\n请联系工程人员在“更多”中登录并设置运行 token。",
                )
                return
            if kind == "full" and self.last_half_sn and sn != self.last_half_sn:
                messagebox.showerror(
                    "SN 不匹配",
                    f"当前 SN 与本机上一次半机测试 SN 不一致，已停止整机测试。\n\n"
                    f"半机 SN：{self.last_half_sn}\n当前 SN：{sn}\n\n请确认产品和 SN。",
                )
                return
        else:
            message = "当前未启用 SN/记录，本次测试不会写入 SN，也不会保存 CSV/测试记录。"
            if auto_flash:
                message += "\n\n注意：本次半机测试会先执行芯片烧录。"
            if not messagebox.askyesno(
                "确认空跑测试",
                f"{message}\n\n是否继续？",
            ):
                return
        self._clear_steps()
        self._set_busy(True)
        self.active_flow_kind = kind
        self.active_flow_sn = sn
        threading.Thread(target=self._flow_worker, args=(kind, sn, token, sn_enabled), daemon=True).start()

    def _flow_worker(self, kind: str, sn: str, token: str, sn_enabled: bool) -> None:
        record = None
        try:
            station = "HALF" if kind == "half" else "FULL"
            if sn_enabled:
                try:
                    storage = RunStorage(
                        self.config_model.records_root,
                        write_extra_files=self.config_model.write_extra_record_files(),
                    )
                    record = storage.start_run(station, sn, self.config_model.dut_alias)
                except Exception as exc:
                    self.events.put(("log", "ERR", f"记录保存失败: {exc}"))
                    self.events.put((
                        "popup",
                        "error",
                        "记录保存失败",
                        "测试尚未开始，记录文件创建失败。\n\n请检查记录目录权限或磁盘空间。",
                    ))
                    return
            else:
                record = NullRunRecord()
                self.events.put(("log", "INFO", "空跑模式：跳过 SN 校验、SN 写入和文件记录"))
                if not token:
                    self.events.put(("log", "INFO", "空跑模式：未填 token，将跳过 Factory unlock/lock"))

            def line_cb(direction: str, line: str) -> None:
                record.log_at(direction, line)
                if direction == "RX" and is_capture_frame_line(line):
                    label = capture_frame_label(line)
                    count = self.frame_line_counts.get(label, 0) + 1
                    self.frame_line_counts[label] = count
                    if count == 1 or count % 10 == 0:
                        self.events.put(("log", "INFO", f"{label}: 已接收 {count} 帧"))
                    return
                # A3: always enqueue; OP filtering happens in UI render path only.
                self.events.put(("log", direction, line))

            def progress(index: int, label: str, status: str, detail: str) -> None:
                self.events.put(("step", index, label, status, detail))

            def before_step(label: str, station_type: str) -> None:
                prompt = self._operator_prompt_for_step(label)
                if prompt is None:
                    return
                title, message, seconds, button_text = prompt
                block_flow = label not in MOMO_TOUCH_STEPS
                done = threading.Event()
                self.events.put(("operator_prompt", title, message, seconds, button_text, done, block_flow))
                if block_flow:
                    done.wait()

            flow_start_index = 1
            if kind == "half" and self.config_model.half_flash_before_test:
                self._close_client_for_flash()
                flash_outcome = self._run_flash_for_flow(record, progress)
                if not flash_outcome.ok:
                    record.finish("NG", f"flash failed: {flash_outcome.message}")
                    self.events.put(("flow_done", FlowOutcome(False, "NG", f"flash failed: {flash_outcome.message}", [])))
                    return
                wait_s = max(0.0, float(self.config_model.flash_after_wait_s))
                if wait_s > 0:
                    self.events.put(("log", "INFO", f"烧录完成，等待设备启动 {wait_s:.1f}s"))
                    time.sleep(wait_s)
                client = self._reconnect_after_flash(record, line_cb, progress)
                if client is None:
                    record.finish("NG", "flash reconnect failed")
                    self.events.put(("flow_done", FlowOutcome(False, "NG", "flash reconnect failed", [])))
                    return
                flow_start_index = 3
            else:
                if self.client is None:
                    raise RuntimeError("device is not connected")
                self.client.set_line_callback(line_cb)

            if kind == "half":
                outcome = run_half_machine(
                    self.client,
                    self.config_model,
                    sn,
                    token,
                    record,
                    progress,
                    sn_enabled=sn_enabled,
                    before_step=before_step,
                    start_index=flow_start_index,
                )  # type: ignore[arg-type]
            else:
                outcome = run_full_machine(
                    self.client,
                    self.config_model,
                    sn,
                    token,
                    record,
                    progress,
                    sn_enabled=sn_enabled,
                    before_step=before_step,
                    start_index=flow_start_index,
                )  # type: ignore[arg-type]
            self.events.put(("flow_done", outcome))
        except Exception as exc:
            if record is not None:
                try:
                    record.finish("NG", str(exc))
                except Exception:
                    pass
            self.events.put(("log", "ERR", f"流程失败: {exc}"))
            self.events.put((
                "popup",
                "error",
                "流程异常",
                f"测试流程异常中断。\n\n错误摘要：{exc}\n\n请检查设备连接、COM 口或 BLE 连接后重新测试。",
            ))
        finally:
            if self.client is not None:
                if self._client_alive():
                    self.client.set_line_callback(self._line_callback)
                else:
                    self.events.put(("connection_lost",))
            self.events.put(("busy", False))

    def _run_ota(self) -> None:
        if self.busy:
            return
        self._save_settings(silent=True)
        image = Path(self.config_model.ota_image_path)
        if not image.exists():
            messagebox.showerror("OTA 包不存在", f"找不到 OTA 包：\n{image}")
            return
        address = self.ble_addr_var.get().strip()
        command = build_ota_command(self.config_model, address)
        if not messagebox.askyesno(
            "确认 OTA 升级",
            "即将执行 OTA 升级，升级过程中请勿断电、断开 BLE 或移动设备。\n\n"
            "OTA 不做固件版本限制。\n\n"
            + " ".join(command.argv),
        ):
            return
        self._set_busy(True)

        def worker() -> None:
            lines: list[str] = []
            try:
                def _ota_log(level: str, line: str) -> None:
                    self.events.put(("log", level, line))
                    lines.append(line)

                code = run_ota(self.config_model, address, _ota_log)
                output = "\n".join(lines)
                same_hash = "matches the active image" in output or "same image hash" in output.lower()
                if code == 0:
                    self.events.put(("log", "OK", f"OTA exit code={code}"))
                elif same_hash:
                    self.events.put(("log", "INFO", "OTA 上传链路已验证：固件 hash 一致，未完成真实 swap"))
                else:
                    self.events.put(("log", "ERR", f"OTA exit code={code}"))
            except Exception as exc:
                self.events.put(("log", "ERR", f"OTA 执行失败: {exc}"))
            finally:
                self.events.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_ble(self) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def worker() -> None:
            try:
                backend = self.ble_scan_backend_var.get().strip() or "nrf_dongle"
                dongle_port = self.ble_dongle_port_var.get().strip() or "COM8"
                self.events.put(("log", "INFO", f"BLE 扫描: backend={backend} dongle={dongle_port}"))
                devices = scan_ble_devices(
                    self.ble_name_var.get().strip() or "AXI-P1-T",
                    8.0,
                    backend=backend,
                    dongle_port=dongle_port,
                    nrf_connect_ble_path=self.nrf_connect_ble_path_var.get().strip(),
                    dongle_sd_version=self.ble_dongle_sd_var.get().strip() or "auto",
                )
                self.events.put(("ble_devices", devices))
            except Exception as exc:
                self.events.put(("log", "ERR", f"BLE 扫描失败: {exc}"))
            finally:
                self.events.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def _use_selected_ble(self) -> None:
        selected = self.ble_tree.selection()
        if not selected:
            return
        values = self.ble_tree.item(selected[0], "values")
        if len(values) >= 2:
            self.ble_name_var.set(values[0])
            self.ble_addr_var.set(values[1])
            self.transport_var.set("BLE")

    def _line_callback(self, direction: str, line: str) -> None:
        if direction == "RX" and is_capture_frame_line(line):
            label = capture_frame_label(line)
            count = self.frame_line_counts.get(label, 0) + 1
            self.frame_line_counts[label] = count
            if count == 1 or count % 10 == 0:
                self.events.put(("log", "INFO", f"{label}: 已接收 {count} 帧"))
            return
        self.events.put(("log", direction, line))

    def _ensure_client(self) -> bool:
        if self.client is not None and not self._client_alive():
            self._close_dead_client()
        if self.client is None:
            messagebox.showwarning("未连接", "请先连接 UART 或 BLE。\n\n开发联调阶段建议优先使用 UART。")
            return False
        return True

    def _format_sn_error(self, reason: str) -> str:
        if reason.startswith("SN too short"):
            return reason.replace("SN too short", "SN 长度太短")
        if reason.startswith("SN too long"):
            return reason.replace("SN too long", "SN 长度太长")
        if reason.startswith("SN must start with "):
            return "SN 前缀不正确，必须以 " + reason[len("SN must start with "):] + " 开头"
        if reason.startswith("SN does not match regex "):
            return "SN 格式不符合正则：" + reason[len("SN does not match regex "):]
        if reason.startswith("SN regex invalid:"):
            return "SN 正则配置无效：" + reason[len("SN regex invalid:"):].strip()
        return reason

    def _operator_prompt_for_step(self, label: str) -> tuple[str, str, int, str] | None:
        if label in MOMO_TOUCH_STEPS:
            return (
                "请触摸 MOMO",
                "请在 5 秒内触摸 MOMO 区域。\n\n检测已经开始，倒计时结束前完成触摸即可。",
                5,
                "",
            )
        return None

    def _show_operator_prompt(self, title: str, message: str, seconds: int, button_text: str, block_flow: bool = True) -> None:
        if not block_flow:
            self._close_active_momo_prompt()
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        if block_flow:
            dialog.grab_set()
        else:
            dialog.attributes("-topmost", True)
            self.active_momo_prompt = dialog

        def close_dialog() -> None:
            if self.active_momo_prompt is dialog:
                self.active_momo_prompt = None
            if dialog.winfo_exists():
                dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        text_var = tk.StringVar(value=message)
        ttk.Label(
            dialog,
            textvariable=text_var,
            font=("Microsoft YaHei UI", 13),
            justify=tk.LEFT,
            wraplength=440,
            padding=(24, 22, 24, 10),
        ).pack(fill=tk.BOTH, expand=True)
        button = None
        if button_text:
            button = ttk.Button(dialog, text=button_text, bootstyle="primary", command=close_dialog)
            button.pack(pady=(0, 22))

        def tick(remaining: int) -> None:
            if not dialog.winfo_exists():
                return
            if remaining <= 0:
                close_dialog()
                return
            text_var.set(f"{message}\n\n倒计时：{remaining} 秒")
            dialog.after(1000, lambda: tick(remaining - 1))

        dialog.update_idletasks()
        x = max(0, self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        if seconds > 0:
            tick(seconds)
        if button is not None:
            button.focus_set()
        if block_flow:
            self.wait_window(dialog)

    def _close_active_momo_prompt(self) -> None:
        dialog = self.active_momo_prompt
        self.active_momo_prompt = None
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()

    def _show_popup(self, level: str, title: str, message: str) -> None:
        if level == "error":
            messagebox.showerror(title, message)
        elif level == "warning":
            messagebox.showwarning(title, message)
        else:
            messagebox.showinfo(title, message)

    def _failed_step_names(self) -> list[str]:
        failed: list[str] = []
        for iid in self.step_tree.get_children():
            status = self.step_status_state.get(str(iid), ("", ""))[1]
            if status not in {"NG", "FAIL", "ERR"}:
                continue
            values = self.step_tree.item(iid, "values")
            if len(values) >= 2:
                failed.append(str(values[1]))
        return failed

    def _has_factory_locked_failure(self) -> bool:
        for iid in self.step_tree.get_children():
            values = self.step_tree.item(iid, "values")
            if len(values) >= 4 and "factory_locked" in str(values[3]):
                return True
        return False

    def _show_flow_done_popup(self, outcome: FlowOutcome) -> None:
        sn = self.sn_var.get().strip()
        sn_text = f"\nSN：{sn}" if self.sn_enabled_var.get() and sn else ""
        if outcome.ok:
            messagebox.showinfo("测试通过", f"本次测试全部通过。{sn_text}")
            return
        if outcome.result == "PENDING-HW":
            messagebox.showwarning("需要继续验证", f"{outcome.message}{sn_text}")
            return
        if self._has_factory_locked_failure() or "factory_locked" in outcome.message:
            messagebox.showerror(
                "设备未解锁",
                "设备处于工厂锁定状态，无法执行硬件测试。\n\n"
                "请确认运行 token 是否与设备端一致，注意连字符 '-' 和下划线 '_' 不同。\n\n"
                "如仍失败，请联系工程人员解锁。",
            )
            return
        failed = self._failed_step_names()
        failed_text = "、".join(failed) if failed else outcome.message
        messagebox.showerror(
            "测试失败",
            f"本次测试未通过。{sn_text}\n\n失败项：{failed_text}\n\n详情：{outcome.message}",
        )

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (self.half_btn, self.full_btn):
            button.configure(state=state)
        self._apply_access_state()

    def _clear_steps(self) -> None:
        for item in self.step_tree.get_children():
            self.step_tree.delete(item)
        for label in self.step_status_labels.values():
            label.destroy()
        self.step_status_labels.clear()
        self.step_status_state.clear()
        self._step_tree_values.clear()
        self._step_label_bbox.clear()
        self._step_status_layout_retries = 0
        if self._step_status_refresh_job is not None:
            try:
                self.after_cancel(self._step_status_refresh_job)
            except tk.TclError:
                pass
            self._step_status_refresh_job = None
        if self._step_status_configure_job is not None:
            try:
                self.after_cancel(self._step_status_configure_job)
            except tk.TclError:
                pass
            self._step_status_configure_job = None

    def _schedule_step_status_refresh(
        self,
        delay_ms: int = UI_STEP_STATUS_REFRESH_MS,
        *,
        force_relayout: bool = False,
    ) -> None:
        if force_relayout:
            self._step_label_bbox.clear()
            self._step_status_layout_retries = 0
            if self._resize_active:
                self._step_status_refresh_deferred = True
                return
        if self._step_status_refresh_job is not None:
            try:
                self.after_cancel(self._step_status_refresh_job)
            except tk.TclError:
                pass
        if delay_ms <= 0:
            self._step_status_refresh_job = self.after_idle(self._refresh_step_status_labels)
        else:
            self._step_status_refresh_job = self.after(delay_ms, self._refresh_step_status_labels)

    def _refresh_step_status_labels(self) -> None:
        self._step_status_refresh_job = None
        pending_layout = False
        for iid, (display_status, status_key) in list(self.step_status_state.items()):
            if not self.step_tree.exists(iid):
                label = self.step_status_labels.pop(iid, None)
                if label is not None:
                    label.destroy()
                self.step_status_state.pop(iid, None)
                self._step_label_bbox.pop(iid, None)
                continue

            color = STEP_STATUS_COLORS.get(status_key)
            if color is None:
                # Non PASS/NG statuses stay as plain black Treeview text.
                label = self.step_status_labels.pop(iid, None)
                if label is not None:
                    label.destroy()
                self._step_label_bbox.pop(iid, None)
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

            if str(label.cget("text")) != display_status or str(label.cget("fg")) != color:
                label.configure(text=display_status, fg=color)
            if self._resize_active:
                self._step_status_refresh_deferred = True
                continue
            bbox = self.step_tree.bbox(iid, "status")
            if bbox:
                x, y, width, height = bbox
                prev = self._step_label_bbox.get(iid)
                if prev != bbox:
                    label.place(x=x + 1, y=y + 1, width=max(0, width - 2), height=max(0, height - 2))
                    self._step_label_bbox[iid] = bbox
            else:
                pending_layout = True
        if pending_layout and self.step_status_state and self._step_status_layout_retries < 2:
            self._step_status_layout_retries += 1
            self._step_status_refresh_job = self.after(16, self._refresh_step_status_labels)
        else:
            self._step_status_layout_retries = 0
            if pending_layout:
                for iid, label in list(self.step_status_labels.items()):
                    if iid in self._step_label_bbox:
                        continue
                    if self.step_tree.bbox(iid, "status"):
                        continue
                    label.place_forget()

    def _put_step(self, idx: int, step: str, status: str, detail: str) -> None:
        iid = str(idx)
        status_key = status.upper()
        display_step = STEP_LABELS_ZH.get(step, step)
        display_status = STEP_STATUS_ZH.get(status_key, status)
        # Keep status text in the tree for non-colored states; colored PASS/NG use overlay.
        tree_status = "" if status_key in STEP_STATUS_COLORS else display_status
        values = (idx, display_step, tree_status, detail)
        dedupe_key = (status_key, detail)
        if self._step_tree_values.get(iid) == dedupe_key and self.step_tree.exists(iid):
            return
        self._step_tree_values[iid] = dedupe_key
        self.step_status_state[iid] = (display_status, status_key)
        if self.step_tree.exists(iid):
            self.step_tree.item(iid, values=values)
        else:
            self.step_tree.insert("", tk.END, iid=iid, values=values)
        self.step_tree.see(iid)
        self._schedule_step_status_refresh()
        if step in MOMO_TOUCH_STEPS and status_key in {"PASS", "OK"}:
            self._close_active_momo_prompt()

    def _should_render_log(self, level: str, message: str) -> bool:
        # A3: OP mode hides verbose AT TX/RX; keep INFO/OK/WARN/ERR and step summaries.
        if self.engineering_mode:
            return True
        if level in {"TX", "RX"}:
            return False
        return True

    def _append_log_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        block = "\n".join(lines) + "\n"
        self.log_text.insert(tk.END, block)
        self._ui_metrics["insert_calls"] += 1
        self._log_line_count += len(lines)
        overflow = self._log_line_count - UI_LOG_MAX_LINES
        if overflow > 0:
            self.log_text.delete("1.0", f"{overflow + 1}.0")
            self._log_line_count = UI_LOG_MAX_LINES
        if self._resize_active:
            self._log_autoscroll_deferred = True
            return
        self._scroll_log_to_end()

    def _scroll_log_to_end(self) -> None:
        self.log_text.see(tk.END)
        self._ui_metrics["see_calls"] += 1

    def _log(self, level: str, message: str) -> None:
        if not self._should_render_log(level, message):
            return
        self._append_log_lines([f"[{level}] {message}"])

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)
        self._log_line_count = 0

    def _dispatch_control_event(self, event: tuple) -> None:
        kind = event[0]
        self._ui_metrics["control_events"] += 1
        if kind == "busy":
            self._set_busy(event[1])
        elif kind == "connected":
            self.client = event[1]
            mode = event[2] if len(event) > 3 else "UART"
            detail = event[3] if len(event) > 3 else event[2]
            self._set_connection_status(mode, detail)
            self._log("OK", f"已连接 {mode} {detail}")
        elif kind == "connection_status":
            self._set_connection_status(event[1], event[2])
        elif kind == "connection_lost":
            self._close_dead_client()
            self._log("WARN", "BLE 连接已断开，请重新连接后再测试")
        elif kind == "step":
            self._put_step(event[1], event[2], event[3], event[4])
        elif kind == "operator_prompt":
            block_flow = event[6] if len(event) > 6 else True
            try:
                self._show_operator_prompt(event[1], event[2], event[3], event[4], block_flow)
            finally:
                event[5].set()
        elif kind == "popup":
            self._show_popup(event[1], event[2], event[3])
        elif kind == "flow_done":
            outcome: FlowOutcome = event[1]
            self._log("OK" if outcome.ok else "ERR", f"流程结束: {outcome.result} {outcome.message}")
            if outcome.ok and self.active_flow_kind == "half" and self.active_flow_sn:
                self.last_half_sn = self.active_flow_sn
            self._show_flow_done_popup(outcome)
        elif kind == "ble_devices":
            self._update_ble_devices(event[1])

    def _poll_events(self) -> None:
        self._ui_metrics["ticks"] += 1
        snapshot: list[tuple] = []
        try:
            while len(snapshot) < UI_EVENT_MAX_DRAIN:
                snapshot.append(self.events.get_nowait())
        except queue.Empty:
            pass

        control_events = [event for event in snapshot if event and event[0] in UI_CONTROL_EVENT_KINDS]
        log_events = [event for event in snapshot if event and event[0] == "log"]
        # Unknown kinds: treat as control so they are not dropped.
        other_events = [
            event
            for event in snapshot
            if event and event[0] not in UI_CONTROL_EVENT_KINDS and event[0] != "log"
        ]

        for event in control_events + other_events:
            if event[0] == "flow_done":
                # Flush any pending visible logs before showing flow result.
                pending_lines = [
                    f"[{item[1]}] {item[2]}"
                    for item in log_events
                    if self._should_render_log(item[1], item[2])
                ]
                self._append_log_lines(pending_lines)
                log_events = []
            self._dispatch_control_event(event)

        rendered: list[str] = []
        for event in log_events:
            self._ui_metrics["log_events"] += 1
            level, message = event[1], event[2]
            if self._should_render_log(level, message):
                rendered.append(f"[{level}] {message}")
        self._append_log_lines(rendered)

        self.after(80, self._poll_events)

    def _update_ble_devices(self, devices: list[BLEDeviceInfo]) -> None:
        self.ble_devices = devices
        for item in self.ble_tree.get_children():
            self.ble_tree.delete(item)
        for idx, dev in enumerate(devices):
            rssi_text = "" if dev.rssi is None else str(dev.rssi)
            self.ble_tree.insert("", tk.END, iid=str(idx), values=(dev.name, dev.address, rssi_text, dev.source))
        self._log("INFO", f"BLE 扫描完成: {len(devices)} 个")

    def _save_settings(self, silent: bool = False) -> None:
        cfg = self.config_model
        cfg.prefer_transport = self.transport_var.get()
        cfg.uart_port = self.uart_port_var.get().strip()
        try:
            cfg.uart_baudrate = int(self.baud_var.get().strip() or "115200")
        except ValueError:
            cfg.uart_baudrate = 115200
        cfg.ble_name = self.ble_name_var.get().strip() or "AXI-P1-T"
        cfg.ble_scan_backend = self.ble_scan_backend_var.get().strip() or "nrf_dongle"
        cfg.ble_dongle_port = self.ble_dongle_port_var.get().strip() or "COM8"
        cfg.ble_dongle_sd_version = self.ble_dongle_sd_var.get().strip() or "auto"
        cfg.nrf_connect_ble_path = self.nrf_connect_ble_path_var.get().strip()
        addr = self.ble_addr_var.get().strip()
        cfg.ble_address_whitelist = [addr] if addr else []
        cfg.station_id = self.station_var.get().strip() or "DEV"
        cfg.sn_enabled = self.sn_enabled_var.get()
        cfg.dut_alias = self.dut_alias_var.get().strip()
        cfg.records_root = self.records_root_var.get().strip()
        cfg.record_output_mode = _record_output_mode(self.record_output_mode_var.get())
        cfg.ota_image_path = self.ota_image_var.get().strip()
        cfg.firmware_repo = self.firmware_repo_var.get().strip()
        cfg.flash_script_path = self.flash_script_var.get().strip()
        cfg.half_flash_before_test = self.half_flash_before_test_var.get()
        cfg.flash_backend = self.flash_backend_var.get().strip() or "nrfjprog"
        cfg.flash_image_path = self.flash_image_var.get().strip()
        cfg.half_flash_image_path = self.half_flash_image_var.get().strip()
        cfg.nrfjprog_path = self.nrfjprog_path_var.get().strip() or "nrfjprog"
        try:
            cfg.flash_after_wait_s = float(self.flash_after_wait_var.get().strip() or "8")
        except ValueError:
            if not silent:
                messagebox.showerror("设置错误", "烧录等待秒数必须是数字")
            return
        cfg.flash_verify = self.flash_verify_var.get()
        cfg.jlink_probe_id = self.jlink_var.get().strip()
        try:
            cfg.sn_rule.min_len = int(self.sn_min_var.get().strip() or "1")
            cfg.sn_rule.max_len = int(self.sn_max_var.get().strip() or "32")
        except ValueError:
            if not silent:
                messagebox.showerror("设置错误", "SN 长度必须是整数")
            return
        cfg.sn_rule.prefix = self.sn_prefix_var.get().strip()
        cfg.sn_rule.regex = self.sn_regex_var.get().strip()
        save_config(cfg)
        self._sync_auth_status()
        self._refresh_flash_text()
        if not silent:
            self._log("OK", "设置已保存")

    def _browse_repo(self) -> None:
        path = filedialog.askdirectory(initialdir=self.firmware_repo_var.get() or str(Path.cwd()))
        if path:
            self.firmware_repo_var.set(path)

    def _browse_records(self) -> None:
        path = filedialog.askdirectory(initialdir=self.records_root_var.get() or str(Path.cwd()))
        if path:
            self.records_root_var.set(path)

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
            parent = path.parent
            if parent != path and parent.exists() and parent.is_dir():
                return str(parent)
        return str(Path.cwd())

    def _browse_flash_script(self) -> None:
        initialdir = self._resolve_browse_dir(self.flash_script_var.get(), self.firmware_repo_var.get())
        path = filedialog.askopenfilename(
            title="选择烧录脚本",
            initialdir=initialdir,
            filetypes=(("PowerShell", "*.ps1"), ("All files", "*.*")),
        )
        if path:
            self.flash_script_var.set(path)

    def _browse_flash_image(self) -> None:
        initialdir = self._resolve_browse_dir(
            self.flash_image_var.get(),
            self.half_flash_image_var.get(),
            self.firmware_repo_var.get(),
        )
        path = filedialog.askopenfilename(
            title="选择固件 hex 文件",
            initialdir=initialdir,
            filetypes=(
                ("Intel HEX 文件", "*.hex"),
                ("所有文件", "*.*"),
            ),
        )
        if path:
            self.flash_image_var.set(path)
            self._refresh_flash_text()

    def _browse_half_flash_image(self) -> None:
        initialdir = self._resolve_browse_dir(
            self.half_flash_image_var.get(),
            self.flash_image_var.get(),
            self.firmware_repo_var.get(),
        )
        path = filedialog.askopenfilename(
            title="选择半机烧录固件 hex 文件",
            initialdir=initialdir,
            filetypes=(
                ("Intel HEX 文件", "*.hex"),
                ("所有文件", "*.*"),
            ),
        )
        if path:
            self.half_flash_image_var.set(path)
            self._refresh_flash_text()

    def _browse_nrfjprog(self) -> None:
        path = filedialog.askopenfilename(filetypes=(("nrfjprog", "nrfjprog.exe"), ("Executable", "*.exe"), ("All files", "*.*")))
        if path:
            self.nrfjprog_path_var.set(path)

    def _browse_ota_image(self) -> None:
        path = filedialog.askopenfilename(filetypes=(("DFU package", "*.zip"), ("All files", "*.*")))
        if path:
            self.ota_image_var.set(path)


def main() -> None:
    app = WorkstationApp()
    try:
        if app.winfo_exists():
            app.mainloop()
    except tk.TclError:
        return
