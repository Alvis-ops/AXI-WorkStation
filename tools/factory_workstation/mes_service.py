from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import MesConfig
from .mes_client import MesHttpClient, MesHttpResult
from .storage import RunSummary


MES_CONFIRMED = "CONFIRMED"
MES_UNCONFIRMED = "UNCONFIRMED"
MES_SKIPPED = "SKIPPED"
MAX_MES_LOG_TEXT = 2000
MES_EXCLUDED_TEST_ITEMS = {
    "Factory unlock",
    "Factory lock",
    "Factory lock cleanup",
}


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


@dataclass(frozen=True)
class MesRunStart:
    status: str
    message: str
    process_started_at: datetime | None = None
    request: dict[str, Any] | None = None
    route_checked: bool = False

    @property
    def confirmed(self) -> bool:
        return self.status == MES_CONFIRMED


@dataclass(frozen=True)
class MesRunCompletion:
    status: str
    message: str
    request: dict[str, Any] | None = None
    pending_path: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == MES_CONFIRMED


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
        raise ValueError(f"MES device_result must be PASS or FAIL, got {result!r}")

    flat_data = dict(data)
    embedded_result = str(flat_data.get("device_result", "")).strip().upper()
    if embedded_result and embedded_result != device_result:
        raise ValueError(
            "MES device_result conflicts with the final device result: "
            f"{embedded_result!r} != {device_result!r}"
        )
    flat_data["device_result"] = device_result

    reserved_fields = {
        config.device_field,
        "Device",
        "Line",
        "Station",
        "SN",
        "ProcessStartTime",
        "ProcessEndTime",
        "Result",
        "Data",
        "ECLIST",
    }
    conflicts = sorted(reserved_fields.intersection(flat_data))
    if conflicts:
        raise ValueError(f"MES top-level fields cannot be overridden by run data: {conflicts}")

    payload = {
        config.device_field: config.device.strip(),
        "Line": config.line.strip(),
        "Station": config.station_name(station_type).strip(),
        "SN": sn.strip(),
        "ProcessStartTime": format_mes_time(process_started_at),
        "ProcessEndTime": format_mes_time(process_ended_at),
    }
    payload.update(flat_data)
    payload["ECLIST"] = list(ec_list or [])
    return payload


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
            "station_type": station_type.strip().upper(),
            "dut_alias": "",
            "device_result": result.strip().upper(),
            "device_message": "MES CLI sample payload",
            "test_items": {},
            "failed_items": {},
        },
        ec_list=[],
    )


def _samples_to_columns(samples: tuple[dict[str, Any], ...]) -> dict[str, list[Any]]:
    columns: dict[str, list[Any]] = {}
    for sample_index, sample in enumerate(samples):
        for values in columns.values():
            values.append(None)
        fields = sample.get("fields", {})
        if not isinstance(fields, dict):
            continue
        for field_name, value in fields.items():
            key = str(field_name)
            if key not in columns:
                columns[key] = [None] * sample_index + [value]
            else:
                columns[key][-1] = value
    return columns


def build_run_data(summary: RunSummary) -> dict[str, Any]:
    mes_device_result = "PASS" if summary.device_result.strip().upper() == "PASS" else "FAIL"
    test_items: dict[str, Any] = {}
    failed_items: dict[str, dict[str, str]] = {}
    duplicate_counts: dict[str, int] = {}
    for item in summary.test_items:
        if item.item_name in MES_EXCLUDED_TEST_ITEMS:
            continue
        base_key = item.item_key
        duplicate_counts[base_key] = duplicate_counts.get(base_key, 0) + 1
        occurrence = duplicate_counts[base_key]
        item_key = base_key if occurrence == 1 else f"{base_key}_{occurrence}"
        test_items[item_key] = {
            "name": item.item_name,
            "result": item.result,
            "elapsed_ms": item.elapsed_ms,
            "error_reason": item.error_reason,
            "response_summary": item.response_summary,
            "measurements": {
                "summary": list(item.measurements),
                "samples": _samples_to_columns(item.samples),
                "sample_count": item.sample_count,
                "uploaded_sample_count": len(item.samples),
                "truncated": item.samples_truncated,
            },
        }
        if item.result.strip().upper() not in {"PASS", "OK", "WARN"}:
            failed_items[item_key] = {
                "item_key": item_key,
                "name": item.item_name,
                "result": item.result,
                "error_reason": item.error_reason,
                "response_summary": item.response_summary,
            }
    return {
        "schema_version": "1.0",
        "run_id": summary.run_id,
        "station_type": summary.station,
        "dut_alias": summary.dut_alias,
        "device_result": mes_device_result,
        "device_message": summary.device_message,
        "test_items": test_items,
        "failed_items": failed_items,
    }


def _operation_log(result: MesOperationResult) -> dict[str, Any]:
    return {
        "accepted": result.accepted,
        "confirmed": result.confirmed,
        "message": result.message,
        "http": {
            "transport_ok": result.http.transport_ok,
            "status_code": result.http.status_code,
            "elapsed_ms": result.http.elapsed_ms,
            "error_kind": result.http.error_kind,
            "message": result.http.message,
            "response_text": result.http.response_text[:MAX_MES_LOG_TEXT],
        },
    }


def start_mes_run(
    config: MesConfig,
    record: Any,
    *,
    sn: str,
    station_type: str,
) -> MesRunStart:
    try:
        service = MesService(config)
        service.validate(station_type, require_response_rule=True)
        if not config.checkroute_enabled:
            record.log_event(
                "mes_checkroute_skipped",
                {"reason": "checkroute is disabled by configuration"},
            )
            return MesRunStart(
                MES_CONFIRMED,
                "checkroute skipped; result upload is enabled",
                process_started_at=datetime.now(),
                route_checked=False,
            )
        request = build_checkroute_payload(config, sn, station_type)
        record.log_event("mes_checkroute_start", {"request": request})
        _request, result = service.checkroute(sn, station_type)
        record.log_event(
            "mes_checkroute_end",
            {"request": request, "result": _operation_log(result)},
        )
        if not result.confirmed:
            return MesRunStart(MES_UNCONFIRMED, result.message, request=request)
        return MesRunStart(
            MES_CONFIRMED,
            result.message,
            process_started_at=datetime.now(),
            request=request,
            route_checked=True,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        try:
            record.log_event("mes_checkroute_end", {"error": message})
        except Exception:
            pass
        return MesRunStart(MES_UNCONFIRMED, message)


def complete_mes_run(
    config: MesConfig,
    record: Any,
    *,
    records_root: str | Path,
    station_type: str,
    process_started_at: datetime,
    device_result: str,
    device_message: str,
) -> MesRunCompletion:
    process_ended_at = datetime.now()
    payload: dict[str, Any] | None = None
    try:
        summary = record.run_summary(
            process_started_at=process_started_at,
            process_ended_at=process_ended_at,
            device_result=device_result,
            device_message=device_message,
        )
        payload = build_postxtdata_payload(
            config,
            sn=summary.sn,
            station_type=station_type,
            process_started_at=process_started_at,
            process_ended_at=process_ended_at,
            result="PASS" if device_result.strip().upper() == "PASS" else "FAIL",
            data=build_run_data(summary),
            ec_list=[],
        )
        record.log_event("mes_post_start", {"request": payload})
        service = MesService(config)
        service.validate(station_type, require_response_rule=True)
        result = service.postxtdata(payload)
        record.log_event(
            "mes_post_end",
            {"request": payload, "result": _operation_log(result)},
        )
        if result.confirmed:
            return MesRunCompletion(MES_CONFIRMED, result.message, request=payload)
        error_message = result.message
    except Exception as exc:
        error_message = str(exc) or exc.__class__.__name__
        try:
            record.log_event("mes_post_end", {"error": error_message})
        except Exception:
            pass

    if payload is None:
        return MesRunCompletion(MES_UNCONFIRMED, error_message)

    try:
        run_id = str(payload.get("run_id", "")) or getattr(record, "run_id", "")
        pending = write_pending_request(
            records_root,
            run_id=run_id,
            url=config.postxtdata_url,
            payload=payload,
            operation="postxtdata",
            last_error=error_message,
        )
        record.log_event(
            "mes_pending",
            {"path": str(pending), "last_error": error_message},
        )
        return MesRunCompletion(
            MES_UNCONFIRMED,
            error_message,
            request=payload,
            pending_path=str(pending),
        )
    except Exception as pending_exc:
        message = f"{error_message}; pending save failed: {pending_exc}"
        try:
            record.log_event("mes_pending_failed", {"error": str(pending_exc)})
        except Exception:
            pass
        return MesRunCompletion(MES_UNCONFIRMED, message, request=payload)


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
