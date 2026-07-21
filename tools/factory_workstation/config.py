from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = PACKAGE_DIR.parent
APP_BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else TOOLS_DIR.parent
FIRMWARE_REPO = TOOLS_DIR.parent.parent / "axi-p1-embeded" if not getattr(sys, "frozen", False) else APP_BASE_DIR
CONFIG_PATH = (APP_BASE_DIR if getattr(sys, "frozen", False) else PACKAGE_DIR) / "config.json"
ENV_PATH = (APP_BASE_DIR if getattr(sys, "frozen", False) else PACKAGE_DIR) / ".env"
_BUNDLED_FLASH_SCRIPT = PACKAGE_DIR / "flash_selected_image.ps1"
_INSTALLED_FLASH_SCRIPT = APP_BASE_DIR / "flash_selected_image.ps1"
DEFAULT_FLASH_SCRIPT = (
    _INSTALLED_FLASH_SCRIPT
    if getattr(sys, "frozen", False) and _INSTALLED_FLASH_SCRIPT.is_file()
    else _BUNDLED_FLASH_SCRIPT
)
TOUCH_CAPTURE_MIN_TIMEOUT_S = 45.0
VIBCAPTURE_MIN_TIMEOUT_S = 60.0
PPG_CAPTURE_MIN_TIMEOUT_S = 45.0


def _default_records_root() -> str:
    return str(APP_BASE_DIR / "factory_records")


@dataclass
class SNRule:
    min_len: int = 1
    max_len: int = 32
    prefix: str = ""
    regex: str = r"^[A-Za-z0-9_-]+$"

    def validate(self, sn: str) -> tuple[bool, str]:
        value = sn.strip()
        if len(value) < self.min_len:
            return False, f"SN too short, min={self.min_len}"
        if len(value) > self.max_len:
            return False, f"SN too long, max={self.max_len}"
        if self.prefix and not value.startswith(self.prefix):
            return False, f"SN must start with {self.prefix}"
        try:
            if self.regex and not re.fullmatch(self.regex, value):
                return False, f"SN does not match regex {self.regex}"
        except re.error as exc:
            return False, f"SN regex invalid: {exc}"
        return True, "OK"


@dataclass
class ATTimeouts:
    default_s: float = 5.0
    unlock_s: float = 8.0
    hw_short_s: float = 12.0
    touch_capture_s: float = TOUCH_CAPTURE_MIN_TIMEOUT_S
    vibcapture_s: float = VIBCAPTURE_MIN_TIMEOUT_S
    ppg_capture_s: float = PPG_CAPTURE_MIN_TIMEOUT_S
    ota_s: float = 300.0

    def touch_capture_timeout_s(self) -> float:
        return max(float(self.touch_capture_s), TOUCH_CAPTURE_MIN_TIMEOUT_S)

    def vibcapture_timeout_s(self) -> float:
        return max(float(self.vibcapture_s), VIBCAPTURE_MIN_TIMEOUT_S)

    def ppg_capture_timeout_s(self) -> float:
        return max(float(self.ppg_capture_s), PPG_CAPTURE_MIN_TIMEOUT_S)

    def for_command(self, command: str) -> float:
        upper = command.upper()
        if upper.startswith("AT+FACTORY=UNLOCK"):
            return self.unlock_s
        if "TOUCH,CAPTURE" in upper:
            return self.touch_capture_timeout_s()
        if "VIBCAPTURE" in upper:
            return self.vibcapture_timeout_s()
        if "PPG,CAPTURE" in upper:
            return self.ppg_capture_timeout_s()
        if upper.startswith("AT+HW="):
            return self.hw_short_s
        return self.default_s


@dataclass
class MesConfig:
    checkroute_enabled: bool = False
    checkroute_url: str = "http://192.168.3.58/json/J.php/xt/checkroute"
    postxtdata_url: str = "http://192.168.3.58/json/J.php/xt/postxtdata"
    device: str = ""
    line: str = ""
    half_station: str = "半机测试"
    full_station: str = "整机测试"
    timeout_s: float = 5.0
    device_field: str = "Device"
    response_success_field: str = "res"
    response_success_values: list[str] = field(
        default_factory=lambda: ["1", "true", "ok", "pass", "success"]
    )
    http_2xx_is_success: bool = False

    def station_name(self, station_type: str) -> str:
        return self.full_station if station_type.strip().upper() == "FULL" else self.half_station

    def validate(self, station_type: str = "HALF") -> tuple[bool, str]:
        if self.checkroute_enabled and not self.checkroute_url.strip():
            return False, "MES checkroute_url is empty"
        if not self.postxtdata_url.strip():
            return False, "MES postxtdata_url is empty"
        if not self.device.strip():
            return False, "MES device is empty"
        if not self.line.strip():
            return False, "MES line is empty"
        if not self.station_name(station_type).strip():
            return False, f"MES station is empty for {station_type}"
        if float(self.timeout_s) <= 0:
            return False, "MES timeout_s must be greater than zero"
        if not self.device_field:
            return False, "MES device_field is empty"
        return True, "OK"

    def has_response_rule(self) -> bool:
        return bool(self.response_success_field.strip() or self.http_2xx_is_success)


_FACTORY_TOKEN_COMMAND_RE = re.compile(
    r"\b(AT\+FACTORY\s*=\s*(?:UNLOCK|EXIT|RECOVER|PRODUCTION|ENTER_PRODUCTION)\s*,\s*)"
    r"([^,\s;|]+)",
    re.IGNORECASE,
)


def redact_sensitive_text(text: str) -> str:
    return _FACTORY_TOKEN_COMMAND_RE.sub(r"\1***", text)


@dataclass
class WorkstationConfig:
    firmware_repo: str = str(FIRMWARE_REPO)
    flash_script_path: str = str(DEFAULT_FLASH_SCRIPT)
    half_flash_before_test: bool = False
    flash_backend: str = "nrfjprog"
    flash_image_path: str = str(FIRMWARE_REPO / "build_ondemand" / "merged.hex")
    half_flash_image_path: str = str(FIRMWARE_REPO / "build_ondemand" / "merged.hex")
    flash_after_wait_s: float = 8.0
    flash_timeout_s: float = 180.0
    flash_verify: bool = True
    nrfjprog_path: str = "nrfjprog"
    jlink_dll_path: str = ""
    jlink_probe_id: str = ""
    uart_port: str = ""
    uart_baudrate: int = 460800
    dut_alias: str = ""
    ble_name: str = "AXI-P1-T"
    ble_address_whitelist: list[str] = field(default_factory=list)
    ble_scan_backend: str = "nrf_dongle"
    ble_pairing_enabled: bool = False
    ble_dongle_port: str = "COM8"
    ble_dongle_sd_version: str = "auto"
    nrf_connect_ble_path: str = ""
    ota_image_path: str = str(FIRMWARE_REPO / "build_ondemand" / "axi-p1-embeded" / "zephyr" / "zephyr.signed.bin")
    ota_enabled: bool = False
    ota_reboot_wait_s: float = 15.0
    records_root: str = field(default_factory=_default_records_root)
    prefer_transport: str = "UART"
    station_id: str = "DEV"
    sn_enabled: bool = True
    capture_output_mode: str = "compact"
    record_output_mode: str = "unified"
    factory_at_required: bool = True
    engineer_password_sha256: str = ""
    sn_rule: SNRule = field(default_factory=SNRule)
    at_timeouts: ATTimeouts = field(default_factory=ATTimeouts)
    mes: MesConfig = field(default_factory=MesConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkstationConfig":
        cfg = cls()
        for key, value in data.items():
            if key == "sn_rule" and isinstance(value, dict):
                cfg.sn_rule = SNRule(**{**asdict(cfg.sn_rule), **value})
            elif key == "at_timeouts" and isinstance(value, dict):
                cfg.at_timeouts = ATTimeouts(**{**asdict(cfg.at_timeouts), **value})
            elif key == "mes" and isinstance(value, dict):
                cfg.mes = MesConfig(**{**asdict(cfg.mes), **value})
            elif hasattr(cfg, key):
                setattr(cfg, key, value)
        if "half_flash_image_path" not in data and "flash_image_path" in data:
            cfg.half_flash_image_path = str(data.get("flash_image_path") or "")
        return cfg

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["factory_token_source"] = "env:AXI_FACTORY_ENGINEER_TOKEN or .env"
        data["recover_token_source"] = "env:AXI_FACTORY_RECOVER_TOKEN or .env"
        return data

    def validate_sn(self, sn: str) -> tuple[bool, str]:
        return self.sn_rule.validate(sn)

    def write_extra_record_files(self) -> bool:
        return str(self.record_output_mode).strip().lower() == "split"


def _dotenv_values(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_PATH
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_dotenv(path: Path | None = None, override: bool = False) -> None:
    for key, value in _dotenv_values(path).items():
        if override or key not in os.environ:
            os.environ[key] = value


def save_dotenv_values(values: dict[str, str], path: Path | None = None) -> None:
    path = path or ENV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for raw in existing_lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            output.append(raw)
            continue
        key, _old_value = line.split("=", 1)
        key = key.strip()
        if key in values:
            output.append(f'{key}="{values[key]}"')
            os.environ[key] = values[key]
            seen.add(key)
        else:
            output.append(raw)

    for key, value in values.items():
        if key not in seen:
            output.append(f'{key}="{value}"')
            os.environ[key] = value

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _resolve_path_field(value: str, base_dir: Path) -> str:
    if not value:
        return value
    path = Path(value)
    return str(path if path.is_absolute() else base_dir / path)


def _resolve_tool_or_path_field(value: str, base_dir: Path) -> str:
    if not value:
        return value
    path = Path(value)
    if path.is_absolute():
        return str(path)
    text = str(value)
    if any(sep in text for sep in ("\\", "/")) or text.startswith("."):
        return str(base_dir / path)
    return value


def _resolve_config_paths(config: WorkstationConfig, base_dir: Path) -> WorkstationConfig:
    config.firmware_repo = _resolve_path_field(config.firmware_repo, base_dir)
    config.flash_script_path = _resolve_path_field(config.flash_script_path, base_dir)
    config.flash_image_path = _resolve_path_field(config.flash_image_path, base_dir)
    config.half_flash_image_path = _resolve_path_field(config.half_flash_image_path, base_dir)
    config.nrfjprog_path = _resolve_tool_or_path_field(config.nrfjprog_path, base_dir)
    config.jlink_dll_path = _resolve_path_field(config.jlink_dll_path, base_dir)
    config.ota_image_path = _resolve_path_field(config.ota_image_path, base_dir)
    config.records_root = _resolve_path_field(config.records_root, base_dir)
    config.nrf_connect_ble_path = _resolve_path_field(config.nrf_connect_ble_path, base_dir)
    return config


def _read_config_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) >= 2 and raw[0] == ord("{") and raw[1] == 0:
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig")


def load_config(path: Path = CONFIG_PATH) -> WorkstationConfig:
    load_dotenv()
    base_dir = path.resolve().parent
    if not path.exists():
        return _resolve_config_paths(WorkstationConfig(), base_dir)
    data = json.loads(_read_config_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a JSON object: {path}")
    return _resolve_config_paths(WorkstationConfig.from_dict(data), base_dir)


def save_config(config: WorkstationConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def get_factory_token(runtime_token: str = "") -> str:
    runtime = runtime_token.strip()
    if runtime:
        return runtime
    dotenv_token = _dotenv_values().get("AXI_FACTORY_ENGINEER_TOKEN", "").strip()
    if dotenv_token:
        os.environ["AXI_FACTORY_ENGINEER_TOKEN"] = dotenv_token
        return dotenv_token
    load_dotenv()
    return os.environ.get("AXI_FACTORY_ENGINEER_TOKEN", "").strip() or os.environ.get("POC3A_FACTORY_TOKEN", "").strip()


def save_factory_token(token: str) -> None:
    value = token.strip()
    if not value:
        raise ValueError("factory token is empty")
    save_dotenv_values({"AXI_FACTORY_ENGINEER_TOKEN": value})


def get_recover_token(runtime_token: str = "") -> str:
    runtime = runtime_token.strip()
    if runtime:
        return runtime
    dotenv_token = _dotenv_values().get("AXI_FACTORY_RECOVER_TOKEN", "").strip()
    if dotenv_token:
        os.environ["AXI_FACTORY_RECOVER_TOKEN"] = dotenv_token
        return dotenv_token
    load_dotenv()
    return os.environ.get("AXI_FACTORY_RECOVER_TOKEN", "").strip() or os.environ.get("POC3A_RECOVER_TOKEN", "").strip()


def has_engineer_password(config: WorkstationConfig | None = None) -> bool:
    """True when any engineer-password source is configured on this station."""
    load_dotenv()
    dotenv = _dotenv_values()
    if (dotenv.get("AXI_FACTORY_ENGINEER_PASSWORD", "") or os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD", "")).strip():
        return True
    expected_hash = (
        dotenv.get("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", "").strip()
        or os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", "").strip()
    )
    if expected_hash:
        return True
    if config is not None and config.engineer_password_sha256.strip():
        return True
    return False


def save_engineer_password(password: str) -> None:
    """Persist engineer password as SHA256 only; clear any plaintext key."""
    value = password.strip()
    if not value:
        raise ValueError("engineer password is empty")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    save_dotenv_values(
        {
            "AXI_FACTORY_ENGINEER_PASSWORD_SHA256": digest,
            "AXI_FACTORY_ENGINEER_PASSWORD": "",
        }
    )


def verify_engineer_password(password: str, config: WorkstationConfig) -> bool:
    load_dotenv()
    value = password.strip()
    if not value:
        return False

    dotenv = _dotenv_values()
    plain = dotenv.get("AXI_FACTORY_ENGINEER_PASSWORD", "") or os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD", "")
    if plain:
        return hmac.compare_digest(value, plain)

    expected_hash = (
        dotenv.get("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", "").strip()
        or os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", "").strip()
        or config.engineer_password_sha256.strip()
    )
    if not expected_hash:
        return False

    actual_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual_hash.lower(), expected_hash.lower())
