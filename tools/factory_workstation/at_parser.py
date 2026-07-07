from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


KV_RE = re.compile(r"([A-Za-z0-9_]+)=([^,]+)")

CAPTURE_FRAME_PREFIXES = (
    "+HW:TOUCH:RAW:",
    "+HW:TOUCH:FILT:",
    "+HW:TOUCH:FRAME:",
    "+HW:IMU:VIBCAPTURE:",
    "+HW:IMU:VIBF:",
    "+HW:PPG:FRAME:",
    "+HW:PPG:F:",
)


@dataclass
class ParsedLine:
    kind: str
    category: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    line: str = ""


def parse_kv(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in KV_RE.finditer(text)}


def split_ints(value: str, count: int | None = None) -> list[int | str]:
    parts = value.split("/") if value else []
    out: list[int | str] = []
    for part in parts:
        item = part.strip()
        try:
            out.append(int(item, 0))
        except ValueError:
            out.append(item)
    if count is not None:
        while len(out) < count:
            out.append("")
    return out


def _parse_positional(prefix: str, text: str, names: list[str]) -> dict[str, str]:
    payload = text[len(prefix):]
    parts = [part.strip() for part in payload.split(",")]
    fields: dict[str, str] = {}
    if len(parts) != len(names):
        return {"parse_error": f"expected {len(names)} fields, got {len(parts)}", "payload": payload}
    for name, value in zip(names, parts):
        fields[name] = value
    return fields


def _expand_slash_field(fields: dict[str, str], source: str, prefix: str, count: int) -> None:
    values = fields.pop(source, "").split("/")
    for index in range(count):
        fields[f"{prefix}{index}"] = values[index].strip() if index < len(values) else ""


def parse_touch_compact(text: str) -> dict[str, str]:
    fields = _parse_positional(
        "+HW:TOUCH:FRAME:",
        text,
        ["seq", "ms", "mask", "stat0", "hostirq", "raw", "diff", "lpf", "baseline"],
    )
    if "parse_error" in fields:
        return fields
    _expand_slash_field(fields, "raw", "raw", 4)
    _expand_slash_field(fields, "diff", "diff", 4)
    _expand_slash_field(fields, "lpf", "lpf", 4)
    _expand_slash_field(fields, "baseline", "baseline", 4)
    return fields


def parse_vib_compact(text: str) -> dict[str, str]:
    return _parse_positional(
        "+HW:IMU:VIBF:",
        text,
        ["seq", "ms", "ax", "ay", "az", "gx", "gy", "gz", "amp_pct"],
    )


def parse_ppg_compact(text: str) -> dict[str, str]:
    return _parse_positional(
        "+HW:PPG:F:",
        text,
        ["seq", "ms", "green0", "red0", "ir0", "green1", "red1", "ir1", "dark0", "dark1", "mask"],
    )


def is_capture_frame_line(line: str) -> bool:
    text = line.strip()
    return any(text.startswith(prefix) for prefix in CAPTURE_FRAME_PREFIXES)


def capture_frame_label(line: str) -> str:
    text = line.strip()
    if text.startswith("+HW:TOUCH:"):
        return "MOMO 空采集"
    if text.startswith("+HW:IMU:"):
        return "LRA 震动采集"
    if text.startswith("+HW:PPG:"):
        return "PPG 反射采集"
    return "采集"


def parse_line(line: str) -> ParsedLine:
    text = line.strip()
    if not text:
        return ParsedLine(kind="empty", line=line)
    if text == "OK":
        return ParsedLine(kind="ok", line=line)
    if text.startswith("+CME ERROR:"):
        return ParsedLine(kind="error", fields={"message": text.split(":", 1)[1].strip()}, line=line)
    if text.startswith("+HW:TOUCH:FRAME:"):
        return ParsedLine(kind="touch_frame", category="touch", fields=parse_touch_compact(text), line=line)
    if text.startswith("+HW:TOUCH:RAW:"):
        return ParsedLine(kind="touch_raw", category="touch", fields=parse_kv(text), line=line)
    if text.startswith("+HW:TOUCH:FILT:"):
        return ParsedLine(kind="touch_filt", category="touch", fields=parse_kv(text), line=line)
    if text.startswith("+HW:TOUCH:RANGE:"):
        return ParsedLine(kind="touch_range", category="touch", fields=parse_kv(text), line=line)
    if text.startswith("+HW:TOUCH:DIFFRANGE:"):
        return ParsedLine(kind="touch_diff_range", category="touch", fields=parse_kv(text), line=line)
    if text.startswith("+HW:TOUCH:CAPTURE:"):
        return ParsedLine(kind="touch_summary", category="touch", fields=parse_kv(text), line=line)
    if text.startswith("+HW:IMU:VIBCAPTURE:"):
        return ParsedLine(kind="vib_frame", category="imu", fields=parse_kv(text), line=line)
    if text.startswith("+HW:IMU:VIBF:"):
        return ParsedLine(kind="vib_frame", category="imu", fields=parse_vib_compact(text), line=line)
    if text.startswith("+HW:IMU:VIBSUMMARY:"):
        return ParsedLine(kind="vib_summary", category="imu", fields=parse_kv(text), line=line)
    if text.startswith("+HW:PPG:F:"):
        return ParsedLine(kind="ppg_frame", category="ppg", fields=parse_ppg_compact(text), line=line)
    if text.startswith("+HW:PPG:FRAME:"):
        return ParsedLine(kind="ppg_frame", category="ppg", fields=parse_kv(text), line=line)
    if text.startswith("+HW:PPG:"):
        parts = text.split(":", 3)
        sub = parts[2].lower() if len(parts) > 2 else "ppg"
        return ParsedLine(kind=f"ppg_{sub}", category="ppg", fields=parse_kv(text), line=line)
    if text.startswith("+HW:RESULT:"):
        return ParsedLine(kind="hw_result", category="hw", fields=parse_kv(text), line=line)
    if text.startswith("+HW:SUMMARY:"):
        return ParsedLine(kind="hw_summary", category="hw", fields=parse_kv(text), line=line)
    if text.startswith("+HW:"):
        return ParsedLine(kind="hw", category="hw", fields=parse_kv(text), line=line)
    if text.startswith("+"):
        head = text[1:].split(":", 1)[0].lower()
        return ParsedLine(kind=head, fields=parse_kv(text), line=line)
    return ParsedLine(kind="text", line=line)


def int_field(fields: dict[str, str], name: str, default: Any = "") -> Any:
    value = fields.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value, 0)
    except ValueError:
        return value
