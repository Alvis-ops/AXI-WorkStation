from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .at_parser import int_field, is_capture_frame_line, parse_line, split_ints
from .config import redact_sensitive_text

# Batch disk flush: avoid per-row flush syscalls on industrial PCs (P1_1 B1/B1b).
CSV_FLUSH_EVERY_ROWS = 50
RAW_LOG_FLUSH_EVERY_LINES = 50


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _safe_name(text: str) -> str:
    value = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text.strip())
    return value or "NA"


@dataclass
class CsvSink:
    path: Path
    fieldnames: list[str]
    flush_every_rows: int = CSV_FLUSH_EVERY_ROWS
    _file: Any = field(default=None, init=False, repr=False)
    _writer: csv.DictWriter | None = field(default=None, init=False, repr=False)
    _rows_since_flush: int = field(default=0, init=False, repr=False)
    flush_count: int = field(default=0, init=False, repr=False)

    def writerow(self, row: dict[str, Any]) -> None:
        if self._writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("w", newline="", encoding="utf-8")
            self._writer = csv.DictWriter(self._file, fieldnames=self.fieldnames, extrasaction="ignore")
            self._writer.writeheader()
            self._rows_since_flush = 0
        clean = {key: row.get(key, "") for key in self.fieldnames}
        self._writer.writerow(clean)
        self._rows_since_flush += 1
        if self._rows_since_flush >= max(1, int(self.flush_every_rows)):
            self.flush()

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()
            self.flush_count += 1
            self._rows_since_flush = 0

    def close(self) -> None:
        if self._file is not None:
            self.flush()
            self._file.close()
            self._file = None
            self._writer = None


class RunRecord:
    def __init__(
        self,
        run_dir: Path,
        station: str,
        sn: str,
        dut_alias: str = "",
        write_extra_files: bool = False,
    ) -> None:
        self.run_dir = run_dir
        self.station = station
        self.sn = sn
        self.dut_alias = dut_alias
        self.run_id = run_dir.name
        self.write_extra_files = write_extra_files
        self._started_monotonic = time.monotonic()
        self._event_index = 0
        self._step_index = ""
        self._step_name = ""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.raw_log = (self.run_dir / "raw_at.log").open("a", encoding="utf-8") if self.write_extra_files else None
        self._raw_log_lines_since_flush = 0
        self.raw_log_flush_count = 0
        self.sinks: dict[str, CsvSink] = {}
        self.meta = {
            "run_id": self.run_id,
            "station": station,
            "sn": sn,
            "dut_alias": dut_alias,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "run_dir": str(run_dir),
        }
        if self.write_extra_files:
            (self.run_dir / "metadata.json").write_text(
                json.dumps(self.meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        self.log_event("run_metadata", self.meta)
        self.log_event("flow_start", {"station": station, "sn": sn, "dut_alias": dut_alias})

    def flush_writes(self) -> None:
        for sink in self.sinks.values():
            sink.flush()
        if self.raw_log is not None:
            self.raw_log.flush()
            self.raw_log_flush_count += 1
            self._raw_log_lines_since_flush = 0

    def log_at(self, direction: str, line: str) -> None:
        logged_line = redact_sensitive_text(line) if direction == "TX" else line
        if self.raw_log is not None:
            self.raw_log.write(f"{_iso()} {direction} {logged_line}\n")
            self._raw_log_lines_since_flush += 1
            if self._raw_log_lines_since_flush >= RAW_LOG_FLUSH_EVERY_LINES:
                self.raw_log.flush()
                self.raw_log_flush_count += 1
                self._raw_log_lines_since_flush = 0
        # B1a: capture frames skip redundant at_rx; ingest_line still runs below.
        skip_at_event = direction == "RX" and is_capture_frame_line(line)
        if not skip_at_event:
            event_type = {"TX": "at_tx", "RX": "at_rx"}.get(direction, direction.lower())
            payload_key = "command" if direction == "TX" else "line"
            self.log_event(event_type, {"direction": direction, payload_key: logged_line}, logged_line)
        if direction == "RX":
            self.ingest_line(line)

    def start_step(self, step_index: int, step_name: str, command: str) -> None:
        self._step_index = str(step_index)
        self._step_name = step_name
        self.log_event(
            "step_start",
            {"step_index": step_index, "step_name": step_name, "command": redact_sensitive_text(command)},
        )

    def log_event(self, event_type: str, payload: dict[str, Any] | None = None, raw_line: str = "") -> None:
        self._event_index += 1
        safe_payload = self._redact_payload(payload or {})
        self._sink(
            "unified_log",
            [
                "run_id",
                "event_index",
                "timestamp_iso",
                "elapsed_ms",
                "station_type",
                "sn",
                "step_index",
                "step_name",
                "event_type",
                "payload_json",
                "raw_line",
            ],
        ).writerow(
            {
                "run_id": self.run_id,
                "event_index": self._event_index,
                "timestamp_iso": _iso(),
                "elapsed_ms": int((time.monotonic() - self._started_monotonic) * 1000),
                "station_type": self.station,
                "sn": self.sn,
                "step_index": self._step_index,
                "step_name": self._step_name,
                "event_type": event_type,
                "payload_json": json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                "raw_line": redact_sensitive_text(raw_line),
            }
        )

    def _redact_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_sensitive_text(value)
        if isinstance(value, dict):
            return {key: self._redact_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_payload(item) for item in value]
        return value

    def log_item(
        self,
        station_type: str,
        item_name: str,
        command: str,
        result: str,
        elapsed_ms: int,
        error_reason: str,
        response_summary: str,
    ) -> None:
        if self.write_extra_files:
            self._sink(
                "factory_test_items",
                [
                    "timestamp",
                    "station_type",
                    "sn",
                    "item_name",
                    "command",
                    "result",
                    "elapsed_ms",
                    "error_reason",
                    "response_summary",
                ],
            ).writerow(
                {
                    "timestamp": _iso(),
                    "station_type": station_type,
                    "sn": self.sn,
                    "item_name": item_name,
                    "command": redact_sensitive_text(command),
                    "result": result,
                    "elapsed_ms": elapsed_ms,
                    "error_reason": error_reason,
                    "response_summary": response_summary,
                }
            )
        self.log_event(
            "step_end",
            {
                "station_type": station_type,
                "item_name": item_name,
                "command": redact_sensitive_text(command),
                "result": result,
                "elapsed_ms": elapsed_ms,
                "error_reason": error_reason,
                "response_summary": response_summary,
            },
        )
        self.flush_writes()

    def finish(self, result: str, details: str = "") -> None:
        self.log_event("flow_end", {"result": result, "details": details})
        self.meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.meta["result"] = result
        self.meta["details"] = details
        if self.write_extra_files:
            (self.run_dir / "metadata.json").write_text(
                json.dumps(self.meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._sink(
                "summary",
                ["timestamp_iso", "station", "sn", "dut_alias", "result", "details"],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "station": self.station,
                    "sn": self.sn,
                    "dut_alias": self.dut_alias,
                    "result": result,
                    "details": details,
                }
            )
        self.flush_writes()
        self.close()

    def close(self) -> None:
        self.flush_writes()
        for sink in self.sinks.values():
            sink.close()
        if self.raw_log is not None:
            self.raw_log.close()
            self.raw_log = None

    def ingest_line(self, line: str) -> None:
        parsed = parse_line(line)
        if parsed.kind not in ("empty", "ok", "text"):
            self.log_event(
                parsed.kind,
                {"kind": parsed.kind, "category": parsed.category, "fields": parsed.fields},
                parsed.line or line,
            )
        if not self.write_extra_files:
            return
        if parsed.kind == "hw_result":
            self._sink(
                "items",
                [
                    "timestamp_iso",
                    "case",
                    "name",
                    "status",
                    "raw0",
                    "raw1",
                    "raw2",
                    "raw3",
                    "line",
                ],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "case": parsed.fields.get("case", ""),
                    "name": parsed.fields.get("name", ""),
                    "status": parsed.fields.get("status", ""),
                    "raw0": parsed.fields.get("raw0", ""),
                    "raw1": parsed.fields.get("raw1", ""),
                    "raw2": parsed.fields.get("raw2", ""),
                    "raw3": parsed.fields.get("raw3", ""),
                    "line": line,
                }
            )
        elif parsed.kind in ("touch_raw", "touch_frame"):
            if parsed.kind == "touch_frame":
                raw = [int_field(parsed.fields, f"raw{index}") for index in range(4)]
                diff = [int_field(parsed.fields, f"diff{index}") for index in range(4)]
            else:
                raw = split_ints(parsed.fields.get("raw", ""), 4)
                diff = split_ints(parsed.fields.get("diff", ""), 4)
            self._sink(
                "momo_raw",
                [
                    "timestamp_iso",
                    "seq",
                    "ms",
                    "mask",
                    "stat0",
                    "hostirq",
                    "raw0",
                    "raw1",
                    "raw2",
                    "raw3",
                    "diff0",
                    "diff1",
                    "diff2",
                    "diff3",
                    "line",
                ],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "seq": int_field(parsed.fields, "seq"),
                    "ms": int_field(parsed.fields, "ms"),
                    "mask": parsed.fields.get("mask", ""),
                    "stat0": parsed.fields.get("stat0", ""),
                    "hostirq": parsed.fields.get("hostirq", ""),
                    "raw0": raw[0],
                    "raw1": raw[1],
                    "raw2": raw[2],
                    "raw3": raw[3],
                    "diff0": diff[0],
                    "diff1": diff[1],
                    "diff2": diff[2],
                    "diff3": diff[3],
                    "line": line,
                }
            )
            if parsed.kind == "touch_frame":
                lpf = [int_field(parsed.fields, f"lpf{index}") for index in range(4)]
                baseline = [int_field(parsed.fields, f"baseline{index}") for index in range(4)]
                self._sink(
                    "momo_filt",
                    [
                        "timestamp_iso",
                        "seq",
                        "lpf0",
                        "lpf1",
                        "lpf2",
                        "lpf3",
                        "baseline0",
                        "baseline1",
                        "baseline2",
                        "baseline3",
                        "line",
                    ],
                ).writerow(
                    {
                        "timestamp_iso": _iso(),
                        "seq": int_field(parsed.fields, "seq"),
                        "lpf0": lpf[0],
                        "lpf1": lpf[1],
                        "lpf2": lpf[2],
                        "lpf3": lpf[3],
                        "baseline0": baseline[0],
                        "baseline1": baseline[1],
                        "baseline2": baseline[2],
                        "baseline3": baseline[3],
                        "line": line,
                    }
                )
        elif parsed.kind == "touch_filt":
            lpf = split_ints(parsed.fields.get("lpf", ""), 4)
            baseline = split_ints(parsed.fields.get("baseline", ""), 4)
            self._sink(
                "momo_filt",
                [
                    "timestamp_iso",
                    "seq",
                    "lpf0",
                    "lpf1",
                    "lpf2",
                    "lpf3",
                    "baseline0",
                    "baseline1",
                    "baseline2",
                    "baseline3",
                    "line",
                ],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "seq": int_field(parsed.fields, "seq"),
                    "lpf0": lpf[0],
                    "lpf1": lpf[1],
                    "lpf2": lpf[2],
                    "lpf3": lpf[3],
                    "baseline0": baseline[0],
                    "baseline1": baseline[1],
                    "baseline2": baseline[2],
                    "baseline3": baseline[3],
                    "line": line,
                }
            )
        elif parsed.kind in ("touch_summary", "touch_range", "touch_diff_range", "vib_summary") or parsed.kind.startswith("ppg_") and parsed.kind != "ppg_frame":
            self._sink(
                "capture_summary",
                ["timestamp_iso", "kind", "fields_json", "line"],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "kind": parsed.kind,
                    "fields_json": json.dumps(parsed.fields, ensure_ascii=False, sort_keys=True),
                    "line": line,
                }
            )
        elif parsed.kind == "vib_frame":
            self._sink(
                "lra_frames",
                [
                    "timestamp_iso",
                    "seq",
                    "ms",
                    "ax",
                    "ay",
                    "az",
                    "gx",
                    "gy",
                    "gz",
                    "amp_pct",
                    "line",
                ],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "seq": int_field(parsed.fields, "seq"),
                    "ms": int_field(parsed.fields, "ms"),
                    "ax": int_field(parsed.fields, "ax"),
                    "ay": int_field(parsed.fields, "ay"),
                    "az": int_field(parsed.fields, "az"),
                    "gx": int_field(parsed.fields, "gx"),
                    "gy": int_field(parsed.fields, "gy"),
                    "gz": int_field(parsed.fields, "gz"),
                    "amp_pct": int_field(parsed.fields, "amp_pct"),
                    "line": line,
                }
            )
        elif parsed.kind == "ppg_frame":
            self._sink(
                "ppg_frames",
                [
                    "timestamp_iso",
                    "seq",
                    "ms",
                    "green0",
                    "red0",
                    "ir0",
                    "green1",
                    "red1",
                    "ir1",
                    "dark0",
                    "dark1",
                    "mask",
                    "map",
                    "line",
                ],
            ).writerow(
                {
                    "timestamp_iso": _iso(),
                    "seq": int_field(parsed.fields, "seq"),
                    "ms": int_field(parsed.fields, "ms"),
                    "green0": int_field(parsed.fields, "green0"),
                    "red0": int_field(parsed.fields, "red0"),
                    "ir0": int_field(parsed.fields, "ir0"),
                    "green1": int_field(parsed.fields, "green1"),
                    "red1": int_field(parsed.fields, "red1"),
                    "ir1": int_field(parsed.fields, "ir1"),
                    "dark0": int_field(parsed.fields, "dark0"),
                    "dark1": int_field(parsed.fields, "dark1"),
                    "mask": parsed.fields.get("mask", ""),
                    "map": parsed.fields.get("map", ""),
                    "line": line,
                }
            )

    def _sink(self, name: str, fieldnames: list[str]) -> CsvSink:
        sink = self.sinks.get(name)
        if sink is None:
            sink = CsvSink(self.run_dir / f"{name}.csv", fieldnames)
            self.sinks[name] = sink
        return sink


class NullRunRecord:
    def log_at(self, direction: str, line: str) -> None:
        return None

    def start_step(self, step_index: int, step_name: str, command: str) -> None:
        return None

    def log_event(self, event_type: str, payload: dict[str, Any] | None = None, raw_line: str = "") -> None:
        return None

    def log_item(
        self,
        station_type: str,
        item_name: str,
        command: str,
        result: str,
        elapsed_ms: int,
        error_reason: str,
        response_summary: str,
    ) -> None:
        return None

    def finish(self, result: str, details: str = "") -> None:
        return None

    def close(self) -> None:
        return None


class RunStorage:
    def __init__(self, root: str | Path, write_extra_files: bool = False) -> None:
        self.root = Path(root)
        self.write_extra_files = write_extra_files

    def start_run(self, station: str, sn: str, dut_alias: str = "") -> RunRecord:
        run_dir = self.root / datetime.now().strftime("%Y-%m-%d") / f"{_stamp()}_{_safe_name(station)}_{_safe_name(sn)}"
        return RunRecord(
            run_dir,
            station=station,
            sn=sn,
            dut_alias=dut_alias,
            write_extra_files=self.write_extra_files,
        )
