from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from pathlib import Path

import ttkbootstrap as ttk

from .at_client import ATClient
from .at_parser import capture_frame_label, is_capture_frame_line
from .config import (
    WorkstationConfig,
    get_factory_token,
    load_config,
    redact_sensitive_text,
    save_factory_token,
    save_config,
    verify_engineer_password,
)
from .flows import FlowOutcome, run_full_machine, run_half_machine
from .ota_runner import build_ota_command, run_ota
from .storage import NullRunRecord, RunStorage
from .transport_ble import BLEDeviceInfo, BLENusTransport, scan_ble_devices
from .transport_uart import UARTTransport, list_serial_ports


STEP_LABELS_ZH = {
    "AT probe": "AT 连通检查",
    "Read version": "读取固件版本",
    "Read capability": "读取能力信息",
    "Factory AT capability": "检查工厂 AT 能力",
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
    "RUN": "#006DFF",
    "PASS": "#00A63E",
    "OK": "#00A63E",
    "WARN": "#C97800",
    "NG": "#E00000",
    "FAIL": "#E00000",
    "ERR": "#E00000",
    "PENDING-HW": "#C97800",
}

MOMO_TOUCH_STEPS = {"Touch ISR"}


class WorkstationApp(ttk.Window):
    def __init__(self) -> None:
        super().__init__(themename="flatly")
        self.title("Axi Factory Workstation")
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = int(screen_width * 0.675)
        height = int(screen_height * 0.675)
        self.geometry(f"{width}x{height}")
        self.minsize(1400, 900)
        self.config_model = load_config()
        self.client: ATClient | None = None
        self.transport_label = tk.StringVar(value="未连接")
        self.busy = False
        self.events: queue.Queue[tuple] = queue.Queue()
        self.frame_line_counts: dict[str, int] = {}
        self.ble_devices: list[BLEDeviceInfo] = []
        self.step_status_labels: dict[str, tk.Label] = {}
        self.step_status_state: dict[str, tuple[str, str]] = {}
        self.active_flow_kind = ""
        self.active_flow_sn = ""
        self.last_half_sn = ""
        self.engineering_mode = False
        self.active_momo_prompt: tk.Toplevel | None = None
        self._build_vars()
        self._build_style()
        self._build_ui()
        self._refresh_ports()
        self._apply_access_state()
        self._center_window()
        self.after(80, self._poll_events)

    def _center_window(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 30)
        self.geometry(f"{width}x{height}+{x}+{y}")

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
        self.ota_image_var = tk.StringVar(value=cfg.ota_image_path)
        self.firmware_repo_var = tk.StringVar(value=cfg.firmware_repo)
        self.flash_script_var = tk.StringVar(value=cfg.flash_script_path)
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
        style.configure(
            "Step.Treeview",
            font=("Microsoft YaHei UI", 11),
            rowheight=36,
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

        left = ttk.Frame(panes, padding=(0, 0, 8, 0), width=380)
        right = ttk.Frame(panes)
        panes.add(left, weight=0)
        panes.add(right, weight=1)

        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self._build_run_tab(self.tabs)
        self._build_ble_tab(self.tabs)
        self._build_settings_tab(self.tabs)
        self._build_more_tab(self.tabs)

        self.right_monitor = ttk.Frame(right)
        self.right_monitor.pack(fill=tk.BOTH, expand=True)
        self._build_monitor(self.right_monitor)

        self.right_help = ttk.Frame(right)
        self._build_help_panel(self.right_help)

        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _build_connection_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="通道").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(bar, textvariable=self.transport_var, values=("UART", "BLE"), width=7, state="readonly").grid(row=0, column=1, padx=(6, 10))
        ttk.Label(bar, text="COM").grid(row=0, column=2, sticky=tk.W)
        self.port_combo = ttk.Combobox(bar, textvariable=self.uart_port_var, width=12)
        self.port_combo.grid(row=0, column=3, padx=(6, 8))
        ttk.Label(bar, text="波特率").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(bar, textvariable=self.baud_var, width=9).grid(row=0, column=5, padx=(6, 8))
        ttk.Button(bar, text="刷新", command=self._refresh_ports, bootstyle="light").grid(row=0, column=6, padx=(0, 12))
        ttk.Label(bar, text="BLE 名").grid(row=0, column=7, sticky=tk.W)
        ttk.Entry(bar, textvariable=self.ble_name_var, width=14).grid(row=0, column=8, padx=(6, 8))
        ttk.Label(bar, text="地址").grid(row=0, column=9, sticky=tk.W)
        ttk.Entry(bar, textvariable=self.ble_addr_var, width=20).grid(row=0, column=10, padx=(6, 12))
        ttk.Button(bar, text="连接", command=self._connect, bootstyle="success").grid(row=0, column=11, padx=(0, 6))
        ttk.Button(bar, text="断开", command=self._disconnect, bootstyle="secondary").grid(row=0, column=12, padx=(0, 12))
        self.connection_status_label = tk.Label(
            bar,
            textvariable=self.transport_label,
            font=("Microsoft YaHei UI", 11, "bold"),
            fg="#FFFFFF",
            bg="#6B7280",
            padx=16,
            pady=6,
            relief=tk.SOLID,
            borderwidth=1,
            width=28,
            anchor=tk.CENTER,
        )
        self.connection_status_label.grid(row=0, column=13, sticky=tk.W)
        bar.columnconfigure(14, weight=1)
        self._set_connection_status("DISCONNECTED", "未连接")

    def _build_run_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs, padding=10)
        tabs.add(frame, text="工厂操作")

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

        ttk.Separator(frame).grid(row=11, column=0, columnspan=2, sticky=tk.EW, pady=12)
        ttk.Label(frame, text="工程调试", style="Title.TLabel").grid(row=12, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        self.manual_cmd_var = tk.StringVar(value="AT")
        self.manual_entry = ttk.Entry(frame, textvariable=self.manual_cmd_var)
        self.manual_entry.grid(row=13, column=0, columnspan=2, sticky=tk.EW, pady=3)
        self.manual_send_btn = ttk.Button(frame, text="发送 AT", bootstyle="primary", command=self._send_manual)
        self.manual_send_btn.grid(row=14, column=0, sticky=tk.EW, pady=3)
        self.probe_btn = ttk.Button(frame, text="探测 AT/VER", bootstyle="secondary", command=self._probe)
        self.probe_btn.grid(row=14, column=1, sticky=tk.EW, padx=(6, 0), pady=3)
        ttk.Label(frame, textvariable=self.manual_hint_var, wraplength=300, foreground="#6B7280").grid(
            row=15,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(4, 0),
        )
        frame.columnconfigure(1, weight=1)
        self._sync_sn_controls()

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
        frame = ttk.Frame(tabs, padding=10)
        self.settings_tab = frame
        tabs.add(frame, text="设置")
        rows = [
            ("固件仓库", self.firmware_repo_var, self._browse_repo),
            ("烧录脚本", self.flash_script_var, self._browse_flash_script),
            ("J-Link", self.jlink_var, None),
            ("记录目录", self.records_root_var, self._browse_records),
            ("OTA 包", self.ota_image_var, self._browse_ota_image),
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
        ttk.Button(frame, text="保存设置", bootstyle="success", command=self._save_settings).grid(row=len(rows), column=0, columnspan=3, sticky=tk.EW, pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def _build_more_tab(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs, padding=10)
        tabs.add(frame, text="更多")

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
        ttk.Label(parent, text="执行步骤", style="Title.TLabel").pack(anchor=tk.W)
        step_frame = ttk.Frame(parent)
        step_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 5))
        self.step_tree = ttk.Treeview(
            step_frame,
            columns=("idx", "step", "status", "detail"),
            show="headings",
            style="Step.Treeview",
        )
        for col, title, width in (
            ("idx", "#", 44),
            ("step", "步骤", 300),
            ("status", "状态", 100),
            ("detail", "详情", 540),
        ):
            self.step_tree.heading(col, text=title)
            self.step_tree.column(col, width=width, stretch=(col == "detail"))
        self.step_tree.bind("<Configure>", lambda _e: self._refresh_step_status_labels())
        self.step_tree.bind("<ButtonRelease-1>", lambda _e: self._refresh_step_status_labels())
        self.step_tree.bind("<KeyRelease>", lambda _e: self._refresh_step_status_labels())
        self.step_tree.bind("<MouseWheel>", lambda _e: self._refresh_step_status_labels())
        self.step_tree.pack(fill=tk.BOTH, expand=True)

        log_header = ttk.Frame(parent)
        log_header.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(log_header, text="AT 日志", style="Title.TLabel").pack(side=tk.LEFT, anchor=tk.W)
        ttk.Button(
            log_header,
            text="清空日志",
            command=self._clear_log,
            style="LogTool.TButton",
            bootstyle="secondary-outline",
        ).pack(side=tk.RIGHT)
        log_frame = ttk.Frame(parent)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log_text = tk.Text(log_frame, height=18, wrap=tk.NONE, font=("Consolas", 10))
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

    def _connect_worker(self) -> None:
        try:
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
                port = self.uart_port_var.get().strip()
                if not port:
                    raise RuntimeError("UART port is empty")
                baudrate = int(self.baud_var.get().strip() or "115200")
                transport = UARTTransport(port, baudrate)
                label = f"{port}@{baudrate}"
            client = ATClient(transport, self._line_callback)
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
            text = f"UART 已连接  {detail}"
            bg = "#008F3A"
        elif state_key == "BLE":
            text = f"BLE 已连接  {detail}"
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

    def _runtime_token_for_flow(self) -> str:
        return get_factory_token("")

    def _sync_auth_status(self) -> None:
        has_token = bool(self._runtime_token_for_flow())
        self.auth_status_var.set("已配置" if has_token else "未配置")

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
            self.tabs.tab(self.settings_tab, state=tk.NORMAL if self.engineering_mode else tk.DISABLED)
        self.manual_hint_var.set(
            "工程模式：允许手动发送 AT 指令，危险操作会写入 AT 日志。"
            if self.engineering_mode
            else "操作员模式：工程调试不可用；测试流程会自动使用隐藏运行授权。"
        )
        self._sync_auth_status()

    def _login_engineering(self) -> None:
        password = simpledialog.askstring("工程登录", "请输入工程密码：", show="*", parent=self)
        if password is None:
            return
        if not verify_engineer_password(password, self.config_model):
            messagebox.showerror(
                "工程登录失败",
                "工程密码不正确，或尚未在 .env / 环境变量中配置工程密码。",
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
        token = simpledialog.askstring("设置运行 token", "请输入设备工厂 token：", show="*", parent=self)
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
        if not self._ensure_client() or self.busy:
            return
        self._save_settings(silent=True)
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
            if not messagebox.askyesno(
                "确认空跑测试",
                "当前未启用 SN/记录，本次测试不会写入 SN，也不会保存 CSV/测试记录。\n\n是否继续？",
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
                    storage = RunStorage(self.config_model.records_root)
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

            self.client.set_line_callback(line_cb)  # type: ignore[union-attr]
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
                "测试流程异常中断。\n\n请检查设备连接、COM 口或 BLE 连接后重新测试。",
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
        if label == "LRA vibcapture":
            return (
                "准备采集震动数据",
                "即将采集 3 秒 LRA 震动数据。\n\n请将设备放稳，采集期间不要移动。",
                0,
                "开始采集",
            )
        if label == "PPG reflect capture":
            return (
                "准备采集 PPG 数据",
                "即将采集 3 秒 PPG 数据。\n\n请按当前测试要求放置设备，采集期间不要移动。",
                0,
                "开始采集",
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
                "请确认运行 token 是否正确，或联系工程人员解锁。",
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

    def _clear_steps(self) -> None:
        for item in self.step_tree.get_children():
            self.step_tree.delete(item)
        for label in self.step_status_labels.values():
            label.destroy()
        self.step_status_labels.clear()
        self.step_status_state.clear()

    def _refresh_step_status_labels(self) -> None:
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

    def _put_step(self, idx: int, step: str, status: str, detail: str) -> None:
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
        self._refresh_step_status_labels()
        if step in MOMO_TOUCH_STEPS and status_key in {"PASS", "OK"}:
            self._close_active_momo_prompt()

    def _log(self, level: str, message: str) -> None:
        self.log_text.insert(tk.END, f"[{level}] {message}\n")
        self.log_text.see(tk.END)

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._log(event[1], event[2])
                elif kind == "busy":
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
        except queue.Empty:
            pass
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
        cfg.ota_image_path = self.ota_image_var.get().strip()
        cfg.firmware_repo = self.firmware_repo_var.get().strip()
        cfg.flash_script_path = self.flash_script_var.get().strip()
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

    def _browse_flash_script(self) -> None:
        path = filedialog.askopenfilename(filetypes=(("PowerShell", "*.ps1"), ("All files", "*.*")))
        if path:
            self.flash_script_var.set(path)

    def _browse_ota_image(self) -> None:
        path = filedialog.askopenfilename(filetypes=(("DFU package", "*.zip"), ("All files", "*.*")))
        if path:
            self.ota_image_var.set(path)


def main() -> None:
    app = WorkstationApp()
    app.mainloop()
