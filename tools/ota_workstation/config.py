from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = PACKAGE_DIR.parents[1]
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PACKAGE_DIR
CONFIG_PATH = APP_DIR / "config.json"


def _default_image_path() -> str:
    if getattr(sys, "frozen", False):
        return str(APP_DIR / "firmware" / "zephyr.signed.bin")
    return str(
        WORKSPACE_DIR.parent
        / "axi-p1-embeded"
        / "build_ondemand"
        / "axi-p1-embeded"
        / "zephyr"
        / "zephyr.signed.bin"
    )


@dataclass
class OtaConfig:
    ble_backend: str = "nrf_dongle"
    ble_name: str = "AXI-P1-T"
    ble_address: str = ""
    ble_pairing_enabled: bool = False
    dongle_port: str = "COM8"
    dongle_sd_version: str = "auto"
    nrf_connect_ble_path: str = ""
    image_path: str = ""
    profile: str = "safe"
    scan_timeout_s: float = 8.0
    reboot_wait_s: float = 15.0
    verify_after_reset: bool = True

    def __post_init__(self) -> None:
        if not self.image_path:
            self.image_path = _default_image_path()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OtaConfig":
        defaults = asdict(cls())
        values = {key: data.get(key, default) for key, default in defaults.items()}
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def normalized_backend(self) -> str:
        backend = str(self.ble_backend or "").strip().lower().replace("-", "_")
        if backend in {"nrf", "dongle", "pc_ble_driver"}:
            return "nrf_dongle"
        if backend in {"bleak", "winrt"}:
            return "windows"
        return backend if backend in {"nrf_dongle", "windows"} else "nrf_dongle"

    def validate(self, *, require_address: bool = True) -> list[str]:
        errors: list[str] = []
        image = Path(str(self.image_path or "").strip())
        if not image.is_file():
            errors.append(f"找不到 OTA 固件：{image}")
        elif image.suffix.lower() not in {".bin", ".zip"}:
            errors.append("OTA 固件必须是签名 .bin 或 DFU .zip 文件")
        if require_address and not str(self.ble_address or "").strip():
            errors.append("请先扫描并选择设备，或手动输入 BLE 地址")
        if self.normalized_backend() == "nrf_dongle" and not str(self.dongle_port or "").strip():
            errors.append("nRF Dongle 模式需要填写 COM 口")
        if self.profile not in {"safe", "balanced"}:
            errors.append("升级速度只能选择 safe 或 balanced")
        try:
            if float(self.scan_timeout_s) <= 0:
                errors.append("扫描超时必须大于 0 秒")
            if float(self.reboot_wait_s) < 0:
                errors.append("重启等待不能小于 0 秒")
        except (TypeError, ValueError):
            errors.append("扫描超时和重启等待必须是数字")
        return errors


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 JSON 对象：{path}")
    return data


def load_config(path: Path = CONFIG_PATH) -> OtaConfig:
    if not path.is_file():
        return OtaConfig()
    config = OtaConfig.from_dict(_read_json(path))
    base = path.resolve().parent
    for field_name in ("image_path", "nrf_connect_ble_path"):
        value = str(getattr(config, field_name) or "").strip()
        if value and not Path(value).is_absolute():
            setattr(config, field_name, str(base / value))
    return config


def save_config(config: OtaConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
