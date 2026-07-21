from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk

# Support both ``python tools/ota_workstation/app.py`` and
# ``python -m tools.ota_workstation.app`` during development.
TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from factory_workstation.transport_ble import BLEDeviceInfo, scan_ble_devices

from .config import OtaConfig, load_config, save_config
from .ota_runner import OtaResult, OtaRunner, build_ota_command


BACKEND_LABELS = {
    "nRF Dongle": "nrf_dongle",
    "Windows 蓝牙": "windows",
}
BACKEND_NAMES = {value: label for label, value in BACKEND_LABELS.items()}
LOG_LINE_LIMIT = 1200


class OtaWorkstationApp(ttk.Window):
    def __init__(self) -> None:
        super().__init__(themename="flatly")
        self.title("AXI OTA 上位机")
        self.geometry("1060x760")
        self.minsize(900, 650)

        try:
            self.config_model = load_config()
        except Exception as exc:
            self.config_model = OtaConfig()
            self.after(100, lambda: messagebox.showwarning("配置读取失败", str(exc)))

        self.events: queue.Queue[tuple] = queue.Queue()
        self.runner = OtaRunner()
        self.busy_mode = ""
        self.devices: list[BLEDeviceInfo] = []
        self._log_lines = 0

        self._build_vars()
        self._build_ui()
        self._apply_backend_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_events)
        self._log("INFO", "独立 OTA 上位机已就绪，请选择固件并扫描设备")

    def _build_vars(self) -> None:
        cfg = self.config_model
        self.backend_var = tk.StringVar(value=BACKEND_NAMES.get(cfg.normalized_backend(), "nRF Dongle"))
        self.name_var = tk.StringVar(value=cfg.ble_name)
        self.address_var = tk.StringVar(value=cfg.ble_address)
        self.pair_var = tk.BooleanVar(value=cfg.ble_pairing_enabled)
        self.dongle_port_var = tk.StringVar(value=cfg.dongle_port)
        self.sd_version_var = tk.StringVar(value=cfg.dongle_sd_version)
        self.nrf_path_var = tk.StringVar(value=cfg.nrf_connect_ble_path)
        self.image_var = tk.StringVar(value=cfg.image_path)
        self.profile_var = tk.StringVar(value=cfg.profile)
        self.scan_timeout_var = tk.StringVar(value=str(cfg.scan_timeout_s))
        self.reboot_wait_var = tk.StringVar(value=str(cfg.reboot_wait_s))
        self.verify_var = tk.BooleanVar(value=cfg.verify_after_reset)
        self.status_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0.0)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=2)
        root.rowconfigure(5, weight=3)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky=tk.EW, pady=(0, 10))
        ttk.Label(header, text="AXI OTA 上位机", font=("Microsoft YaHei UI", 19, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="BLE SMP / MCUboot", bootstyle="secondary").pack(side=tk.LEFT, padx=14)
        ttk.Label(header, textvariable=self.status_var, bootstyle="primary", font=("Microsoft YaHei UI", 11, "bold")).pack(side=tk.RIGHT)

        settings = ttk.Labelframe(root, text="升级设置", padding=10)
        settings.grid(row=1, column=0, sticky=tk.EW)
        for col in (1, 3, 5):
            settings.columnconfigure(col, weight=1)

        ttk.Label(settings, text="BLE 后端").grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.backend_combo = ttk.Combobox(
            settings,
            textvariable=self.backend_var,
            values=tuple(BACKEND_LABELS),
            width=15,
            state="readonly",
        )
        self.backend_combo.grid(row=0, column=1, sticky=tk.EW, padx=(0, 14), pady=4)
        self.backend_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_backend_state())

        ttk.Label(settings, text="广播名").grid(row=0, column=2, sticky=tk.W, padx=(0, 6), pady=4)
        ttk.Entry(settings, textvariable=self.name_var).grid(row=0, column=3, sticky=tk.EW, padx=(0, 14), pady=4)
        ttk.Label(settings, text="BLE 地址").grid(row=0, column=4, sticky=tk.W, padx=(0, 6), pady=4)
        ttk.Entry(settings, textvariable=self.address_var).grid(row=0, column=5, sticky=tk.EW, pady=4)

        ttk.Label(settings, text="Dongle COM").grid(row=1, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.dongle_port_entry = ttk.Entry(settings, textvariable=self.dongle_port_var, width=16)
        self.dongle_port_entry.grid(row=1, column=1, sticky=tk.EW, padx=(0, 14), pady=4)
        ttk.Label(settings, text="SoftDevice API").grid(row=1, column=2, sticky=tk.W, padx=(0, 6), pady=4)
        self.sd_combo = ttk.Combobox(
            settings,
            textvariable=self.sd_version_var,
            values=("auto", "v2", "v3", "v5"),
            state="readonly",
        )
        self.sd_combo.grid(row=1, column=3, sticky=tk.EW, padx=(0, 14), pady=4)
        self.pair_check = ttk.Checkbutton(settings, text="Windows 认证/配对", variable=self.pair_var, bootstyle="round-toggle")
        self.pair_check.grid(row=1, column=4, columnspan=2, sticky=tk.W, pady=4)

        ttk.Label(settings, text="nRF Connect BLE").grid(row=2, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.nrf_path_entry = ttk.Entry(settings, textvariable=self.nrf_path_var)
        self.nrf_path_entry.grid(row=2, column=1, columnspan=4, sticky=tk.EW, padx=(0, 8), pady=4)
        self.nrf_browse_btn = ttk.Button(settings, text="浏览", command=self._browse_nrf_path, bootstyle="secondary-outline", width=8)
        self.nrf_browse_btn.grid(row=2, column=5, sticky=tk.E, pady=4)

        ttk.Label(settings, text="OTA 固件").grid(row=3, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        ttk.Entry(settings, textvariable=self.image_var).grid(row=3, column=1, columnspan=4, sticky=tk.EW, padx=(0, 8), pady=4)
        ttk.Button(settings, text="浏览", command=self._browse_image, bootstyle="secondary-outline", width=8).grid(row=3, column=5, sticky=tk.E, pady=4)

        options = ttk.Frame(settings)
        options.grid(row=4, column=0, columnspan=6, sticky=tk.EW, pady=(5, 0))
        ttk.Label(options, text="速度").pack(side=tk.LEFT)
        ttk.Combobox(options, textvariable=self.profile_var, values=("safe", "balanced"), width=11, state="readonly").pack(side=tk.LEFT, padx=(6, 18))
        ttk.Label(options, text="扫描超时(s)").pack(side=tk.LEFT)
        ttk.Entry(options, textvariable=self.scan_timeout_var, width=7).pack(side=tk.LEFT, padx=(6, 18))
        ttk.Label(options, text="重启等待(s)").pack(side=tk.LEFT)
        ttk.Entry(options, textvariable=self.reboot_wait_var, width=7).pack(side=tk.LEFT, padx=(6, 18))
        ttk.Checkbutton(options, text="重启后校验镜像", variable=self.verify_var, bootstyle="round-toggle").pack(side=tk.LEFT)

        toolbar = ttk.Frame(root)
        toolbar.grid(row=2, column=0, sticky=tk.EW, pady=10)
        self.scan_btn = ttk.Button(toolbar, text="扫描设备", command=self._scan, bootstyle="info", width=14)
        self.scan_btn.pack(side=tk.LEFT)
        self.upgrade_btn = ttk.Button(toolbar, text="开始 OTA", command=self._start_ota, bootstyle="warning", width=14)
        self.upgrade_btn.pack(side=tk.LEFT, padx=8)
        self.stop_btn = ttk.Button(toolbar, text="中止", command=self._cancel_ota, bootstyle="danger-outline", width=10, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)
        ttk.Button(toolbar, text="保存设置", command=self._save, bootstyle="secondary-outline", width=11).pack(side=tk.RIGHT)

        device_frame = ttk.Labelframe(root, text="扫描结果（双击选择）", padding=6)
        device_frame.grid(row=3, column=0, sticky=tk.NSEW)
        device_frame.rowconfigure(0, weight=1)
        device_frame.columnconfigure(0, weight=1)
        columns = ("name", "address", "rssi", "source")
        self.device_tree = ttk.Treeview(device_frame, columns=columns, show="headings", height=7)
        self.device_tree.heading("name", text="设备名")
        self.device_tree.heading("address", text="BLE 地址")
        self.device_tree.heading("rssi", text="RSSI")
        self.device_tree.heading("source", text="来源")
        self.device_tree.column("name", width=190)
        self.device_tree.column("address", width=240)
        self.device_tree.column("rssi", width=80, anchor=tk.CENTER)
        self.device_tree.column("source", width=220)
        scroll = ttk.Scrollbar(device_frame, command=self.device_tree.yview)
        self.device_tree.configure(yscrollcommand=scroll.set)
        self.device_tree.grid(row=0, column=0, sticky=tk.NSEW)
        scroll.grid(row=0, column=1, sticky=tk.NS)
        self.device_tree.bind("<Double-1>", lambda _event: self._select_device())

        progress_frame = ttk.Frame(root)
        progress_frame.grid(row=4, column=0, sticky=tk.EW, pady=10)
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, bootstyle="success-striped")
        self.progress.grid(row=0, column=0, sticky=tk.EW)
        self.progress_label = ttk.Label(progress_frame, text="0.0%", width=8, anchor=tk.E)
        self.progress_label.grid(row=0, column=1, padx=(8, 0))

        log_frame = ttk.Labelframe(root, text="运行日志", padding=6)
        log_frame.grid(row=5, column=0, sticky=tk.NSEW)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 10),
            bg="#101820",
            fg="#E8EEF2",
            insertbackground="white",
        )
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        log_scroll.grid(row=0, column=1, sticky=tk.NS)

    def _collect_config(self) -> OtaConfig | None:
        try:
            scan_timeout = float(self.scan_timeout_var.get().strip())
            reboot_wait = float(self.reboot_wait_var.get().strip())
        except ValueError:
            messagebox.showerror("设置错误", "扫描超时和重启等待必须是数字")
            return None
        return OtaConfig(
            ble_backend=BACKEND_LABELS.get(self.backend_var.get(), "nrf_dongle"),
            ble_name=self.name_var.get().strip() or "AXI-P1-T",
            ble_address=self.address_var.get().strip(),
            ble_pairing_enabled=self.pair_var.get(),
            dongle_port=self.dongle_port_var.get().strip() or "COM8",
            dongle_sd_version=self.sd_version_var.get().strip() or "auto",
            nrf_connect_ble_path=self.nrf_path_var.get().strip(),
            image_path=self.image_var.get().strip(),
            profile=self.profile_var.get().strip() or "safe",
            scan_timeout_s=scan_timeout,
            reboot_wait_s=reboot_wait,
            verify_after_reset=self.verify_var.get(),
        )

    def _save(self, silent: bool = False) -> bool:
        cfg = self._collect_config()
        if cfg is None:
            return False
        try:
            save_config(cfg)
            self.config_model = cfg
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return False
        if not silent:
            self._log("OK", "设置已保存")
        return True

    def _apply_backend_state(self) -> None:
        is_dongle = BACKEND_LABELS.get(self.backend_var.get(), "nrf_dongle") == "nrf_dongle"
        dongle_state = tk.NORMAL if is_dongle else tk.DISABLED
        for widget in (self.dongle_port_entry, self.nrf_path_entry, self.nrf_browse_btn):
            widget.configure(state=dongle_state)
        self.sd_combo.configure(state="readonly" if is_dongle else tk.DISABLED)
        self.pair_check.configure(state=tk.DISABLED if is_dongle else tk.NORMAL)
        if is_dongle:
            self.pair_var.set(False)

    def _set_busy(self, mode: str) -> None:
        self.busy_mode = mode
        busy = bool(mode)
        self.scan_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.upgrade_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.stop_btn.configure(state=tk.NORMAL if mode == "ota" else tk.DISABLED)
        if not busy:
            self.progress.stop()

    def _scan(self) -> None:
        if self.busy_mode:
            return
        cfg = self._collect_config()
        if cfg is None:
            return
        if cfg.scan_timeout_s <= 0:
            messagebox.showerror("设置错误", "扫描超时必须大于 0 秒")
            return
        self._set_busy("scan")
        self.status_var.set("正在扫描…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._log("INFO", f"开始扫描：backend={cfg.normalized_backend()} name={cfg.ble_name}")

        def worker() -> None:
            try:
                devices = scan_ble_devices(
                    cfg.ble_name,
                    cfg.scan_timeout_s,
                    backend=cfg.normalized_backend(),
                    dongle_port=cfg.dongle_port,
                    nrf_connect_ble_path=cfg.nrf_connect_ble_path,
                    dongle_sd_version=cfg.dongle_sd_version,
                )
                self.events.put(("devices", devices))
            except Exception as exc:
                self.events.put(("error", f"BLE 扫描失败：{exc}"))
            finally:
                self.events.put(("idle", "扫描完成"))

        threading.Thread(target=worker, daemon=True).start()

    def _start_ota(self) -> None:
        if self.busy_mode:
            return
        cfg = self._collect_config()
        if cfg is None:
            return
        errors = cfg.validate(require_address=True)
        if errors:
            messagebox.showerror("无法开始 OTA", "\n".join(f"• {item}" for item in errors))
            return
        command = build_ota_command(cfg, cfg.ble_address)
        image = Path(cfg.image_path)
        size_text = f"{image.stat().st_size / 1024:.1f} KiB"
        backend_name = BACKEND_NAMES.get(cfg.normalized_backend(), cfg.normalized_backend())
        if not messagebox.askyesno(
            "确认 OTA 升级",
            f"目标：{cfg.ble_name}  {cfg.ble_address}\n"
            f"后端：{backend_name}\n"
            f"固件：{image.name}（{size_text}）\n"
            f"Helper：{command.helper_name}\n\n"
            "升级过程中请勿断电或移走设备，是否继续？",
        ):
            return
        self.config_model = cfg
        save_config(cfg)
        self.progress.configure(mode="determinate")
        self.progress_var.set(0.0)
        self.progress_label.configure(text="0.0%")
        self.status_var.set("正在升级…")
        self._set_busy("ota")
        self._log("INFO", f"开始 OTA：{image} -> {cfg.ble_address}")
        self._log("INFO", f"OTA Helper：{command.helper_name}，速度：{cfg.profile}")

        def worker() -> None:
            try:
                result = self.runner.run(
                    cfg,
                    cfg.ble_address,
                    lambda line: self.events.put(("ota_log", line)),
                    lambda value: self.events.put(("progress", value)),
                )
                self.events.put(("result", result))
            except Exception as exc:
                self.events.put(("result", OtaResult("failed", -1, f"OTA 执行失败：{exc}")))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_ota(self) -> None:
        if self.busy_mode != "ota":
            return
        if not messagebox.askyesno("确认中止", "中止传输可能使本次升级失败，但不会删除当前活动固件。是否中止？"):
            return
        if self.runner.cancel():
            self.status_var.set("正在中止…")
            self._log("WARN", "操作员请求中止 OTA")

    def _select_device(self) -> None:
        selection = self.device_tree.selection()
        if not selection:
            return
        values = self.device_tree.item(selection[0], "values")
        if len(values) >= 2:
            self.name_var.set(str(values[0]))
            self.address_var.set(str(values[1]))
            self._log("INFO", f"已选择设备：{values[0]} {values[1]}")

    def _update_devices(self, devices: list[BLEDeviceInfo]) -> None:
        self.devices = devices
        for item in self.device_tree.get_children():
            self.device_tree.delete(item)
        for index, device in enumerate(devices):
            self.device_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(device.name, device.address, "" if device.rssi is None else device.rssi, device.source),
            )
        self._log("OK", f"扫描完成，共发现 {len(devices)} 个匹配设备")
        if len(devices) == 1:
            self.device_tree.selection_set("0")
            self._select_device()

    def _handle_result(self, result: OtaResult) -> None:
        self._set_busy("")
        if result.status == "success":
            self.progress_var.set(100.0)
            self.progress_label.configure(text="100.0%")
            self.status_var.set("升级成功")
            self._log("OK", result.message)
            messagebox.showinfo("OTA 完成", result.message)
        elif result.status == "same_hash":
            self.status_var.set("固件相同")
            self._log("WARN", result.message)
            messagebox.showwarning("未发生升级", result.message)
        elif result.status == "cancelled":
            self.status_var.set("已中止")
            self._log("WARN", result.message)
        else:
            self.status_var.set("升级失败")
            self._log("ERR", result.message)
            messagebox.showerror("OTA 失败", result.message + "\n\n请查看运行日志定位 BLE、SMP 或固件问题。")

    def _poll_events(self) -> None:
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "devices":
                self._update_devices(event[1])
            elif kind == "ota_log":
                self._log("OTA", event[1])
            elif kind == "progress":
                value = float(event[1])
                self.progress_var.set(value)
                self.progress_label.configure(text=f"{value:.1f}%")
            elif kind == "error":
                self._log("ERR", event[1])
                messagebox.showerror("操作失败", event[1])
            elif kind == "idle":
                if self.busy_mode == "scan":
                    self._set_busy("")
                    self.progress.configure(mode="determinate")
                    self.progress_var.set(0.0)
                    self.progress_label.configure(text="0.0%")
                    self.status_var.set(event[1])
            elif kind == "result":
                self._handle_result(event[1])
        self.after(80, self._poll_events)

    def _log(self, level: str, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{level}] {message}\n")
        self._log_lines += 1
        if self._log_lines > LOG_LINE_LIMIT:
            self.log_text.delete("1.0", "201.0")
            self._log_lines -= 200
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _browse_image(self) -> None:
        current = Path(self.image_var.get())
        initial = current.parent if current.parent.is_dir() else Path.cwd()
        path = filedialog.askopenfilename(
            title="选择 OTA 固件",
            initialdir=str(initial),
            filetypes=(("OTA 固件", "*.bin *.zip"), ("签名固件", "*.bin"), ("DFU 包", "*.zip"), ("所有文件", "*.*")),
        )
        if path:
            self.image_var.set(path)

    def _browse_nrf_path(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 nRF Connect BLE 程序",
            filetypes=(("nRF Connect BLE", "*.exe"), ("所有文件", "*.*")),
        )
        if path:
            self.nrf_path_var.set(path)

    def _on_close(self) -> None:
        if self.busy_mode == "ota":
            messagebox.showwarning("OTA 正在运行", "请先等待升级完成或点击“中止”。")
            return
        self._save(silent=True)
        self.destroy()


def main() -> None:
    app = OtaWorkstationApp()
    app.mainloop()
