from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = PACKAGE_DIR.parent
APP_BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else TOOLS_DIR.parent
CONFIG_PATH = (APP_BASE_DIR if getattr(sys, "frozen", False) else PACKAGE_DIR) / "config.json"


def _default_records_root() -> str:
    return str(APP_BASE_DIR / "bare_board_records")


@dataclass
class SNRule:
    min_len: int = 1
    max_len: int = 48
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
class BareBoardConfig:
    firmware_repo: str = str(APP_BASE_DIR)
    flash_backend: str = "nrfjprog"
    flash_image_path: str = str(APP_BASE_DIR / "bare_board_test.hex")
    flash_script_path: str = ""
    nrfjprog_path: str = "nrfjprog"
    nrfjprog_family: str = "NRF54L15_XXAA"
    jlink_probe_id: str = ""
    flash_verify: bool = True
    flash_timeout_s: float = 180.0
    flash_after_wait_s: float = 2.0
    serial_port: str = ""
    serial_baudrate: int = 460800
    serial_timeout_s: float = 60.0
    serial_open_wait_s: float = 0.5
    test_start_command: str = ""
    pass_patterns: list[str] = field(default_factory=lambda: ["PASS", "TEST PASS", "RESULT:PASS"])
    fail_patterns: list[str] = field(default_factory=lambda: ["FAIL", "NG", "ERROR", "RESULT:FAIL"])
    end_patterns: list[str] = field(default_factory=lambda: ["TEST DONE", "END", "RESULT:"])
    records_root: str = field(default_factory=_default_records_root)
    station_id: str = "BARE"
    sn_rule: SNRule = field(default_factory=SNRule)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BareBoardConfig":
        cfg = cls()
        for key, value in data.items():
            if key == "sn_rule" and isinstance(value, dict):
                cfg.sn_rule = SNRule(**{**asdict(cfg.sn_rule), **value})
            elif hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate_sn(self, sn: str) -> tuple[bool, str]:
        return self.sn_rule.validate(sn)


def _read_config_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) >= 2 and raw[0] == ord("{") and raw[1] == 0:
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig")


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


def _resolve_config_paths(config: BareBoardConfig, base_dir: Path) -> BareBoardConfig:
    config.firmware_repo = _resolve_path_field(config.firmware_repo, base_dir)
    config.flash_image_path = _resolve_path_field(config.flash_image_path, base_dir)
    config.flash_script_path = _resolve_path_field(config.flash_script_path, base_dir)
    config.nrfjprog_path = _resolve_tool_or_path_field(config.nrfjprog_path, base_dir)
    config.records_root = _resolve_path_field(config.records_root, base_dir)
    return config


def load_config(path: Path = CONFIG_PATH) -> BareBoardConfig:
    base_dir = path.resolve().parent
    if not path.exists():
        return _resolve_config_paths(BareBoardConfig(), base_dir)
    data = json.loads(_read_config_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a JSON object: {path}")
    return _resolve_config_paths(BareBoardConfig.from_dict(data), base_dir)


def save_config(config: BareBoardConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
