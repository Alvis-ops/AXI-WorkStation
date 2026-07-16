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
MAX_STEP_MEASUREMENTS = 32

TEST_ITEM_KEYS = {
    "Firmware flash": "firmware_flash",
    "Flash reconnect": "flash_reconnect",
    "Factory AT capability": "factory_at_capability",
    "AT probe": "at_probe",
    "Read version": "firmware_version",
    "Write SN": "sn_write",
    "Read SN": "sn_read",
    "SN persistence check": "sn_persistence",
    "Read OTA busy": "ota_busy",
    "Power path": "power_path",
    "IMU communication": "imu_communication",
    "Touch communication": "touch_communication",
    "Charger communication": "charger_communication",
    "Gauge communication": "gauge_communication",
    "Flash communication": "storage_flash_communication",
    "PPG communication": "ppg_communication",
    "PPG dark capture": "ppg_dark_capture",
    "Touch ISR": "touch_isr",
    "Touch capture": "touch_capture",
    "LRA vibcapture": "lra_vibcapture",
    "PPG reflect capture": "ppg_reflect_capture",
    "OTA transport check": "ota_transport_check",
    "OTA version before": "ota_version_before",
    "OTA busy check": "ota_busy_check",
    "OTA image check": "ota_image_check",
    "OTA disconnect NUS": "ota_disconnect_nus",
    "OTA upload": "ota_upload",
    "OTA reconnect NUS": "ota_reconnect_nus",
    "OTA state check": "ota_state_check",
    "OTA busy after same-hash": "ota_busy_after_same_hash",
    "OTA reboot wait": "ota_reboot_wait",
    "OTA busy after reboot": "ota_busy_after_reboot",
    "OTA version after": "ota_version_after",
}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _safe_name(text: str) -> str:
    value = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text.strip())
    return value or "NA"


def _item_key(item_name: str) -> str:
    configured = TEST_ITEM_KEYS.get(item_name)
    if configured:
        return configured
    value = "".join(ch.lower() if ch.isalnum() else "_" for ch in item_name.strip())
    return "_".join(part for part in value.split("_") if part) or "unknown_item"


@dataclass(frozen=True)
class RecordedTestItem:
    item_key: str
    item_name: str
    result: str
    elapsed_ms: int
    error_reason: str
    response_summary: str
    measurements: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    station: str
    sn: str
    dut_alias: str
    process_started_at: str
    process_ended_at: str
    device_result: str
    device_message: str
    test_items: tuple[RecordedTestItem, ...]


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
        self._step_measurements: list[dict[str, Any]] = []
        self._test_items: list[RecordedTestItem] = []
        self._finished = False
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
        self._step_measurements = []
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
        self._test_items.append(
            RecordedTestItem(
                item_key=_item_key(item_name),
                item_name=item_name,
                result=result,
                elapsed_ms=int(elapsed_ms),
                error_reason=error_reason,
                response_summary=response_summary,
                measurements=tuple(dict(item) for item in self._step_measurements),
            )
        )
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
        self._step_measurements = []
        self.flush_writes()

    def run_summary(
        self,
        *,
        process_started_at: datetime | str,
        process_ended_at: datetime | str,
        device_result: str,
        device_message: str,
    ) -> RunSummary:
        def iso(value: datetime | str) -> str:
            return value.isoformat(timespec="seconds") if isinstance(value, datetime) else str(value)

        return RunSummary(
            run_id=self.run_id,
            station=self.station,
            sn=self.sn,
            dut_alias=self.dut_alias,
            process_started_at=iso(process_started_at),
            process_ended_at=iso(process_ended_at),
            device_result=device_result,
            device_message=device_message,
            test_items=tuple(self._test_items),
        )

    def finish(
        self,
        result: str,
        details: str = "",
        *,
        mes_status: str = "",
        mes_details: str = "",
        mes_pending_path: str = "",
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self.log_event(
            "flow_end",
            {
                "result": result,
                "details": details,
                "mes_status": mes_status,
                "mes_details": mes_details,
                "mes_pending_path": mes_pending_path,
            },
        )
        self.meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.meta["result"] = result
        self.meta["details"] = details
        self.meta["mes_status"] = mes_status
        self.meta["mes_details"] = mes_details
        self.meta["mes_pending_path"] = mes_pending_path
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
        if (
            parsed.fields
            and parsed.kind not in {"empty", "ok", "text", "touch_frame", "vib_frame", "ppg_frame"}
            and len(self._step_measurements) < MAX_STEP_MEASUREMENTS
        ):
            self._step_measurements.append(
                {
                    "kind": parsed.kind,
                    "category": parsed.category,
                    "fields": dict(parsed.fields),
                    "line": (parsed.line or line)[:1000],
                }
            )
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
    run_id = ""
    station = ""
    sn = ""
    dut_alias = ""

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

    def run_summary(
        self,
        *,
        process_started_at: datetime | str,
        process_ended_at: datetime | str,
        device_result: str,
        device_message: str,
    ) -> RunSummary:
        return RunSummary(
            run_id="",
            station="",
            sn="",
            dut_alias="",
            process_started_at=str(process_started_at),
            process_ended_at=str(process_ended_at),
            device_result=device_result,
            device_message=device_message,
            test_items=(),
        )

    def finish(
        self,
        result: str,
        details: str = "",
        *,
        mes_status: str = "",
        mes_details: str = "",
        mes_pending_path: str = "",
    ) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class HalfSnRecordCheck:
    ok: bool
    message: str
    record_path: str = ""
    result: str = ""


def _half_sn_candidate(
    *,
    path: Path,
    station: str,
    sn: str,
    target_sn: str,
    result: str,
    details: str = "",
    timestamp: str = "",
) -> dict[str, Any] | None:
    if station.strip().upper() != "HALF":
        return None
    if sn.strip() != target_sn:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "path": str(path),
        "result": result.strip().upper(),
        "details": details.strip(),
        "timestamp": timestamp.strip(),
        "mtime": mtime,
    }


def _half_sn_candidates_from_unified(path: Path, target_sn: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                if row.get("event_type", "").strip() != "flow_end":
                    continue
                payload: dict[str, Any] = {}
                try:
                    payload = json.loads(row.get("payload_json", "") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                candidate = _half_sn_candidate(
                    path=path,
                    station=row.get("station_type", ""),
                    sn=row.get("sn", ""),
                    target_sn=target_sn,
                    result=str(payload.get("result", "")),
                    details=str(payload.get("details", "")),
                    timestamp=row.get("timestamp_iso", ""),
                )
                if candidate is not None:
                    candidates.append(candidate)
    except OSError:
        return []
    return candidates


def _half_sn_candidates_from_summary(path: Path, target_sn: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            for row in csv.DictReader(file):
                candidate = _half_sn_candidate(
                    path=path,
                    station=row.get("station", ""),
                    sn=row.get("sn", ""),
                    target_sn=target_sn,
                    result=row.get("result", ""),
                    details=row.get("details", ""),
                    timestamp=row.get("timestamp_iso", ""),
                )
                if candidate is not None:
                    candidates.append(candidate)
    except OSError:
        return []
    return candidates


def _half_sn_candidates_from_metadata(path: Path, target_sn: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    candidate = _half_sn_candidate(
        path=path,
        station=str(data.get("station", "")),
        sn=str(data.get("sn", "")),
        target_sn=target_sn,
        result=str(data.get("result", "")),
        details=str(data.get("details", "")),
        timestamp=str(data.get("finished_at") or data.get("started_at") or ""),
    )
    return [candidate] if candidate is not None else []


def verify_half_sn_pass_record(records_root: str | Path, sn: str) -> HalfSnRecordCheck:
    target_sn = sn.strip()
    root = Path(records_root)
    if not target_sn:
        return HalfSnRecordCheck(False, "SN 为空，无法校验半机记录")
    if not root.exists():
        return HalfSnRecordCheck(False, f"记录目录不存在，无法校验半机记录：{root}")

    candidates: list[dict[str, Any]] = []
    for unified in root.rglob("unified_log.csv"):
        candidates.extend(_half_sn_candidates_from_unified(unified, target_sn))
    for summary in root.rglob("summary.csv"):
        candidates.extend(_half_sn_candidates_from_summary(summary, target_sn))
    for metadata in root.rglob("metadata.json"):
        candidates.extend(_half_sn_candidates_from_metadata(metadata, target_sn))

    if not candidates:
        return HalfSnRecordCheck(
            False,
            f"未找到 SN {target_sn} 的半机测试记录；请先完成半机测试，或确认半机/整机使用同一个记录目录：{root}",
        )

    latest = max(candidates, key=lambda item: (float(item.get("mtime", 0.0)), str(item.get("timestamp", ""))))
    result = str(latest.get("result", ""))
    record_path = str(latest.get("path", ""))
    if result != "PASS":
        details = str(latest.get("details", ""))
        suffix = f"；详情：{details}" if details else ""
        return HalfSnRecordCheck(
            False,
            f"SN {target_sn} 最新半机测试记录不是 PASS，而是 {result or 'UNKNOWN'}{suffix}；记录：{record_path}",
            record_path,
            result,
        )
    return HalfSnRecordCheck(True, f"SN {target_sn} 已找到半机 PASS 记录", record_path, result)


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
