from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import CONFIG_PATH, BareBoardConfig, load_config, save_config
from .flow import run_bare_board_test
from .serial_runner import list_serial_ports


class BareBoardApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Bare Board Workstation")
        self.geometry("1040x680")
        self.minsize(880, 560)
        self.config_path = CONFIG_PATH
        self.config_model = load_config(self.config_path)
        self.events: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()

        self.sn_var = tk.StringVar()
        self.backend_var = tk.StringVar(value=self.config_model.flash_backend)
        self.port_var = tk.StringVar(value=self.config_model.serial_port)
        self.baud_var = tk.StringVar(value=str(self.config_model.serial_baudrate))
        self.image_var = tk.StringVar(value=self.config_model.flash_image_path)
        self.probe_var = tk.StringVar(value=self.config_model.jlink_probe_id)
        self.family_var = tk.StringVar(value=self.config_model.nrfjprog_family)
        self.start_cmd_var = tk.StringVar(value=self.config_model.test_start_command)
        self.records_var = tk.StringVar(value=self.config_model.records_root)
        self.timeout_var = tk.StringVar(value=str(self.config_model.serial_timeout_s))
        self.status_var = tk.StringVar(value="READY")
        self.record_var = tk.StringVar(value="")

        self._build_style()
        self._build_layout()
        self._refresh_ports()
        self.after(80, self._drain_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Action.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 8))

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Bare Board Workstation", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")

        form = ttk.Frame(self, padding=(16, 6, 8, 16))
        form.grid(row=1, column=0, sticky="nsw")
        for index in range(3):
            form.columnconfigure(index, weight=1 if index == 1 else 0)

        row = 0
        ttk.Label(form, text="SN").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.sn_var, width=34).grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
        row += 1

        ttk.Label(form, text="烧录方式").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=self.backend_var, values=["nrfjprog", "script"], width=24).grid(
            row=row, column=1, columnspan=2, sticky="ew", pady=5
        )
        row += 1

        ttk.Label(form, text="串口").grid(row=row, column=0, sticky="w", pady=5)
        self.port_combo = ttk.Combobox(form, textvariable=self.port_var, width=24)
        self.port_combo.grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(form, text="刷新", command=self._refresh_ports).grid(row=row, column=2, padx=(6, 0), pady=5)
        row += 1

        ttk.Label(form, text="波特率").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.baud_var, width=12).grid(row=row, column=1, sticky="ew", pady=5)
        row += 1

        ttk.Label(form, text="固件").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.image_var, width=28).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(form, text="选择", command=self._browse_image).grid(row=row, column=2, padx=(6, 0), pady=5)
        row += 1

        ttk.Label(form, text="Probe").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.probe_var, width=28).grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
        row += 1

        ttk.Label(form, text="Family").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.family_var, width=28).grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
        row += 1

        ttk.Label(form, text="启动命令").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.start_cmd_var, width=28).grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
        row += 1

        ttk.Label(form, text="超时(s)").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.timeout_var, width=12).grid(row=row, column=1, sticky="ew", pady=5)
        row += 1

        ttk.Label(form, text="记录").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.records_var, width=28).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(form, text="选择", command=self._browse_records).grid(row=row, column=2, padx=(6, 0), pady=5)
        row += 1

        actions = ttk.Frame(form)
        actions.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(16, 8))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(actions, text="开始", style="Action.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        row += 1

        ttk.Button(form, text="保存配置", command=self._save_config).grid(row=row, column=0, columnspan=3, sticky="ew")
        row += 1

        record_frame = ttk.Frame(form)
        record_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        record_frame.columnconfigure(0, weight=1)
        ttk.Label(record_frame, textvariable=self.record_var, wraplength=320).grid(row=0, column=0, sticky="w")

        log_frame = ttk.Frame(self, padding=(8, 6, 16, 16))
        log_frame.grid(row=1, column=1, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="none", height=24, font=("Consolas", 10), relief="solid", borderwidth=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        values = [port.device for port in ports]
        self.port_combo["values"] = values
        if not self.port_var.get() and values:
            self.port_var.set(values[0])

    def _browse_image(self) -> None:
        path = filedialog.askopenfilename(
            title="选择固件",
            filetypes=[("Firmware", "*.hex *.bin"), ("All files", "*.*")],
        )
        if path:
            self.image_var.set(path)

    def _browse_records(self) -> None:
        path = filedialog.askdirectory(title="选择记录目录")
        if path:
            self.records_var.set(path)

    def _current_config(self) -> BareBoardConfig:
        config = load_config(self.config_path)
        config.flash_backend = self.backend_var.get().strip() or "nrfjprog"
        config.serial_port = self.port_var.get().strip()
        config.serial_baudrate = int(self.baud_var.get().strip() or "460800")
        config.flash_image_path = self.image_var.get().strip()
        config.jlink_probe_id = self.probe_var.get().strip()
        config.nrfjprog_family = self.family_var.get().strip()
        config.test_start_command = self.start_cmd_var.get().strip()
        config.records_root = self.records_var.get().strip()
        config.serial_timeout_s = float(self.timeout_var.get().strip() or "60")
        return config

    def _save_config(self) -> None:
        try:
            config = self._current_config()
            save_config(config, self.config_path)
            self.status_var.set("SAVED")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        sn = self.sn_var.get().strip()
        try:
            config = self._current_config()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        ok, reason = config.validate_sn(sn)
        if not ok:
            messagebox.showerror("SN 错误", reason)
            return
        self.stop_event.clear()
        self.log_text.delete("1.0", tk.END)
        self.record_var.set("")
        self.status_var.set("RUNNING")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        self.worker = threading.Thread(target=self._run_worker, args=(config, sn), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("STOPPING")

    def _run_worker(self, config: BareBoardConfig, sn: str) -> None:
        def line(direction: str, text: str) -> None:
            self.events.put(("line", direction, text))

        def progress(step: str, status: str, detail: str) -> None:
            self.events.put(("progress", step, f"{status} {detail}".rstrip()))

        outcome = run_bare_board_test(
            config,
            sn,
            line_callback=line,
            progress_callback=progress,
            stop_event=self.stop_event,
        )
        self.events.put(("done", outcome.result, f"{outcome.message}|{outcome.record_path}"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, left, right = self.events.get_nowait()
                if kind == "line":
                    self._append_log(f"[{left}] {right}")
                elif kind == "progress":
                    self._append_log(f"[STEP] {left}: {right}")
                elif kind == "done":
                    result, payload = left, right
                    message, _, path = payload.partition("|")
                    self.status_var.set(result)
                    if path:
                        self.record_var.set(path)
                    self._append_log(f"[RESULT] {result} {message}")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _append_log(self, line: str) -> None:
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)


def main() -> None:
    app = BareBoardApp()
    app.mainloop()
