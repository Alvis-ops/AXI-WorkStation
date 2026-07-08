from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BareBoardConfig


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _safe_name(text: str) -> str:
    value = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text.strip())
    return value or "NA"


@dataclass
class TestRecord:
    path: Path
    config: BareBoardConfig
    sn: str
    _started_monotonic: float = field(default_factory=time.monotonic, init=False, repr=False)
    _file: Any = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def open(self) -> "TestRecord":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", newline="\n")
        self.write_header()
        return self

    def write_header(self) -> None:
        self._write("# Bare Board Test Record")
        self._write(f"started_at={_iso()}")
        self._write(f"station_id={self.config.station_id}")
        self._write(f"sn={self.sn}")
        self._write(f"flash_backend={self.config.flash_backend}")
        self._write(f"flash_image_path={self.config.flash_image_path}")
        self._write(f"nrfjprog_family={self.config.nrfjprog_family}")
        self._write(f"jlink_probe_id={self.config.jlink_probe_id}")
        self._write(f"serial_port={self.config.serial_port}")
        self._write(f"serial_baudrate={self.config.serial_baudrate}")
        self._write("---")

    def log(self, category: str, line: str) -> None:
        elapsed_ms = int((time.monotonic() - self._started_monotonic) * 1000)
        self._write(f"{_iso()} +{elapsed_ms:07d}ms [{category}] {line}")

    def log_metadata(self, key: str, value: object) -> None:
        self.log("META", f"{key}={value}")

    def finish(self, result: str, details: str = "") -> None:
        self._write("---")
        self._write(f"finished_at={_iso()}")
        self._write(f"result={result}")
        self._write(f"details={details}")
        self.close()

    def close(self) -> None:
        if self._file is not None and not self._closed:
            self._file.close()
            self._closed = True

    def _write(self, line: str) -> None:
        if self._file is None:
            raise RuntimeError("record file is not open")
        self._file.write(f"{line}\n")
        self._file.flush()


class NullRecord:
    path = Path("")

    def log(self, category: str, line: str) -> None:
        return None

    def log_metadata(self, key: str, value: object) -> None:
        return None

    def finish(self, result: str, details: str = "") -> None:
        return None

    def close(self) -> None:
        return None


class RecordStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def start_run(self, config: BareBoardConfig, sn: str) -> TestRecord:
        record_dir = self.root / datetime.now().strftime("%Y-%m-%d")
        path = record_dir / f"{_stamp()}_{_safe_name(config.station_id)}_{_safe_name(sn)}.log"
        return TestRecord(path=path, config=config, sn=sn).open()
