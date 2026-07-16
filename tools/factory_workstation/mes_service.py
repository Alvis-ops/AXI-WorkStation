from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import MesConfig
from .mes_client import MesHttpClient, MesHttpResult


@dataclass(frozen=True)
class MesOperationResult:
    accepted: bool | None
    message: str
    http: MesHttpResult

    @property
    def confirmed(self) -> bool:
        return self.accepted is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "confirmed": self.confirmed,
            "message": self.message,
            "http": self.http.to_dict(),
        }


def _lookup_json_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        key = part.strip()
        if not key:
            continue
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _normalized(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def evaluate_response(config: MesConfig, result: MesHttpResult) -> MesOperationResult:
    if not result.transport_ok:
        return MesOperationResult(False, result.message or "MES HTTP request failed", result)

    success_field = config.response_success_field.strip()
    if success_field:
        actual = _lookup_json_path(result.response_json, success_field)
        if actual is None:
            return MesOperationResult(
                False,
                f"MES response field is missing: {success_field}",
                result,
            )
        allowed = {_normalized(item) for item in config.response_success_values}
        accepted = _normalized(actual) in allowed
        return MesOperationResult(
            accepted,
            f"{success_field}={actual!r}",
            result,
        )

    if config.http_2xx_is_success:
        return MesOperationResult(True, result.message or "MES HTTP request accepted", result)

    return MesOperationResult(
        None,
        "MES HTTP succeeded, but the business success rule is not configured",
        result,
    )


def format_mes_time(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value)
    return parsed.strftime("%Y%m%d%H%M")


def build_checkroute_payload(config: MesConfig, sn: str, station_type: str = "HALF") -> dict[str, Any]:
    return {
        config.device_field: config.device.strip(),
        "Line": config.line.strip(),
        "Station": config.station_name(station_type).strip(),
        "SN": sn.strip(),
    }


def build_postxtdata_payload(
    config: MesConfig,
    *,
    sn: str,
    station_type: str,
    process_started_at: datetime | str,
    process_ended_at: datetime | str,
    result: str,
    data: dict[str, Any],
    ec_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    device_result = result.strip().upper()
    if device_result not in {"PASS", "FAIL"}:
        raise ValueError(f"MES Result must be PASS or FAIL, got {result!r}")
    return {
        config.device_field: config.device.strip(),
        "Line": config.line.strip(),
        "Station": config.station_name(station_type).strip(),
        "SN": sn.strip(),
        "ProcessStartTime": format_mes_time(process_started_at),
        "ProcessEndTime": format_mes_time(process_ended_at),
        "Result": device_result,
        "Data": data,
        "ECLIST": list(ec_list or []),
    }


def build_sample_post_payload(
    config: MesConfig,
    *,
    sn: str,
    station_type: str = "HALF",
    result: str = "PASS",
) -> dict[str, Any]:
    now = datetime.now()
    return build_postxtdata_payload(
        config,
        sn=sn,
        station_type=station_type,
        process_started_at=now,
        process_ended_at=now,
        result=result,
        data={
            "schema_version": "1.0",
            "run_id": f"MES_CLI_{now.strftime('%Y%m%d_%H%M%S')}_{sn.strip()}",
            "test_items": {},
        },
        ec_list=[],
    )


def _safe_run_id(run_id: str) -> str:
    value = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in run_id.strip())
    return value or "UNKNOWN_RUN"


def write_pending_request(
    records_root: str | Path,
    *,
    run_id: str,
    url: str,
    payload: dict[str, Any],
    operation: str,
    last_error: str,
    attempts: int = 1,
) -> Path:
    pending_dir = Path(records_root) / "mes_pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    target = pending_dir / f"{_safe_run_id(run_id)}.json"
    document = {
        "schema_version": 1,
        "run_id": run_id,
        "operation": operation,
        "url": url,
        "payload": payload,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "attempts": int(attempts),
        "last_attempt_at": datetime.now().isoformat(timespec="seconds"),
        "last_error": last_error,
    }
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}_",
        suffix=".tmp",
        dir=str(pending_dir),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(document, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return target


class MesService:
    def __init__(self, config: MesConfig, client: MesHttpClient | None = None) -> None:
        self.config = config
        self.client = client or MesHttpClient()

    def validate(self, station_type: str = "HALF", *, require_response_rule: bool = False) -> None:
        ok, reason = self.config.validate(station_type)
        if not ok:
            raise ValueError(reason)
        if require_response_rule and not self.config.has_response_rule():
            raise ValueError(
                "MES business success rule is not configured; set response_success_field "
                "or explicitly enable http_2xx_is_success"
            )

    def checkroute(self, sn: str, station_type: str = "HALF") -> tuple[dict[str, Any], MesOperationResult]:
        self.validate(station_type)
        payload = build_checkroute_payload(self.config, sn, station_type)
        response = self.client.post_json(
            self.config.checkroute_url,
            payload,
            timeout_s=self.config.timeout_s,
        )
        return payload, evaluate_response(self.config, response)

    def postxtdata(self, payload: dict[str, Any]) -> MesOperationResult:
        response = self.client.post_json(
            self.config.postxtdata_url,
            payload,
            timeout_s=self.config.timeout_s,
        )
        return evaluate_response(self.config, response)
