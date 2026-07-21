from __future__ import annotations

import csv
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factory_workstation.at_client import ATClient
    from factory_workstation.cli import run as run_cli
    from factory_workstation.config import MesConfig, WorkstationConfig, load_config, save_config
    from factory_workstation.mes_client import MesHttpClient
    from factory_workstation.mes_service import (
        MesService,
        build_run_data,
        build_postxtdata_payload,
        evaluate_response,
        write_pending_request,
    )
    from factory_workstation.storage import MAX_STEP_SAMPLES, RunRecord
else:
    from .at_client import ATClient
    from .cli import run as run_cli
    from .config import MesConfig, WorkstationConfig, load_config, save_config
    from .mes_client import MesHttpClient
    from .mes_service import (
        MesService,
        build_run_data,
        build_postxtdata_payload,
        evaluate_response,
        write_pending_request,
    )
    from .storage import MAX_STEP_SAMPLES, RunRecord


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _MesHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    success_by_path: dict[str, bool] = {}

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append({"path": self.path, "payload": payload})
        success = self.__class__.success_by_path.get(self.path, True)
        body = json.dumps(
            {
                "tag": 418,
                "res": "OK" if success else "NG",
                "msg": "OK" if success else "拒绝",
                "ec": 1 if success else 0,
                "data": {"result": "接口测试"},
                "log": [],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return None


class _Server:
    def __init__(self, success_by_path: dict[str, bool] | None = None) -> None:
        self.success_by_path = dict(success_by_path or {})

    def __enter__(self):
        _MesHandler.requests = []
        _MesHandler.success_by_path = self.success_by_path
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MesHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


class _ScriptedTransport:
    def __init__(self, responses: dict[str, list[str]]) -> None:
        self.responses = responses
        self.pending: list[str] = []
        self.commands: list[str] = []

    def clear_input(self) -> None:
        self.pending.clear()

    def write_line(self, command: str) -> None:
        self.commands.append(command)
        self.pending = list(self.responses.get(command, ["+CME ERROR: 99,missing_smoke_response"]))

    def read_line(self, _timeout_s: float) -> str | None:
        return self.pending.pop(0) if self.pending else None

    def close(self) -> None:
        return None


PPG_DARK_CAPTURE_CMD = "AT+HW=PPG,CAPTURE,CONFIRM,DARK,1000,100,COMPACT"
TOUCH_CAPTURE_CMD = "AT+HW=TOUCH,CAPTURE,CONFIRM,3000,COMPACT"
VIB_CAPTURE_CMD = "AT+HW=IMU,VIBCAPTURE,CONFIRM,50,3000,20,COMPACT"
PPG_REFLECT_CAPTURE_CMD = "AT+HW=PPG,CAPTURE,CONFIRM,REFLECT,3000,50,COMPACT"


def _half_responses() -> dict[str, list[str]]:
    return {
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=0.0.1,build=mes-smoke", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        "AT+SN=SN001": ["OK"],
        "AT+SN?": ["+SN:value=SN001,valid=1,source=lfs,production=0,ret=0", "OK"],
        "AT+HW=POWER": ["+HW:POWER:status=PASS,vbat_mv=4021", "OK"],
        "AT+HW=IMU,PROBE": ["+HW:IMU:PROBE:status=PASS,whoami=71", "OK"],
        "AT+HW=TOUCH,PROBE": ["+HW:TOUCH:PROBE:status=PASS", "OK"],
        "AT+HW=CHG,REGS": ["+HW:CHG:REGS:status=PASS,vbus_mv=5000", "OK"],
        "AT+HW=GAUGE,DATA": ["+HW:GAUGE:DATA:status=PASS,soc=88", "OK"],
        "AT+HW=FLASH,PROBE": ["+HW:FLASH:PROBE:status=PASS,jedec=0x1234", "OK"],
        "AT+HW=PPG,PROBE": ["+HW:PPG:PROBE:status=PASS", "OK"],
        PPG_DARK_CAPTURE_CMD: [
            "+HW:PPG:F:0,50,1,2,3,4,5,6,7,8,0x03",
            "+HW:PPG:CAPTURE:samples=10,status=PASS,dark0=7,dark1=8",
            "OK",
        ],
        "AT+HW=TOUCH,ISR,CONFIRM": ["+HW:TOUCH:ISR:status=PASS", "OK"],
        "AT+FACTORY=LOCK": ["OK"],
    }


def _full_responses() -> dict[str, list[str]]:
    return {
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=0.0.1,build=mes-smoke", "OK"],
        "AT+SN?": ["+SN:value=SN001,valid=1,source=lfs,production=0,ret=0", "OK"],
        "AT+OTABUSY?": ["+OTABUSY:locked=0", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        TOUCH_CAPTURE_CMD: [
            "+HW:TOUCH:FRAME:0,0,0x02,0x00000000,0x00000001,100/101/102/103,1/2/3/4,90/91/92/93,80/81/82/83",
            "+HW:TOUCH:CAPTURE:samples=60,status=PASS,min=80,max=103",
            "OK",
        ],
        VIB_CAPTURE_CMD: [
            "+HW:IMU:VIBF:0,20,12,-3,998,1,0,-1,50",
            "+HW:IMU:VIBSUMMARY:samples=150,duration_ms=3000,status=PASS,peak=998",
            "OK",
        ],
        PPG_REFLECT_CAPTURE_CMD: [
            "+HW:PPG:F:0,50,123,456,789,124,457,790,10,11,0x03",
            "+HW:PPG:CAPTURE:samples=60,status=PASS,green_avg=123",
            "OK",
        ],
        "AT+FACTORY=LOCK": ["OK"],
    }


def _mes_config(base_url: str) -> MesConfig:
    return MesConfig(
        checkroute_url=f"{base_url}/checkroute",
        postxtdata_url=f"{base_url}/postxtdata",
        device="DE0001",
        line="L1",
        response_success_field="res",
    )


def test_http_utf8_and_response_rule() -> None:
    with _Server() as server:
        config = _mes_config(server.base_url)
        payload, result = MesService(config).checkroute("测试SN001", "HALF")
        _assert(result.confirmed, f"checkroute was not confirmed: {result}")
        _assert(payload["Station"] == "半机测试", "station name was not UTF-8")
        _assert(_MesHandler.requests[-1]["payload"]["SN"] == "测试SN001", "SN was not UTF-8")


def test_unconfigured_business_rule_is_unknown() -> None:
    with _Server() as server:
        config = _mes_config(server.base_url)
        config.response_success_field = ""
        raw = MesHttpClient().post_json(config.checkroute_url, {"SN": "SN001"})
        result = evaluate_response(config, raw)
        _assert(result.accepted is None, "unconfigured response rule must not guess MES success")


def test_flat_post_payload_with_nested_items() -> None:
    config = MesConfig(device="DE0001", line="L1")
    payload = build_postxtdata_payload(
        config,
        sn="SN001",
        station_type="FULL",
        process_started_at="2026-07-16T10:00:00",
        process_ended_at="2026-07-16T10:01:00",
        result="FAIL",
        data={
            "schema_version": "1.0",
            "run_id": "RUN001",
            "test_items": {
                "ppg_capture": {
                    "result": "FAIL",
                    "measurements": {
                        "summary": [],
                        "samples": {"green0": ["1"], "red0": ["2"]},
                        "sample_count": 1,
                        "uploaded_sample_count": 1,
                        "truncated": False,
                    },
                    "logs": [{"direction": "RX", "line": "+HW:PPG:F:..."}],
                }
            },
        },
        ec_list=[{"ERROR_CODE": "M107", "LOCATION": "U10"}],
    )
    _assert(payload["Station"] == "整机测试", "full station mapping failed")
    _assert("Data" not in payload, "legacy Data wrapper must not be uploaded")
    _assert("Result" not in payload, "legacy Result field must not be uploaded")
    _assert(payload["device_result"] == "FAIL", "top-level device result was not normalized")
    samples = payload["test_items"]["ppg_capture"]["measurements"]["samples"]
    _assert(samples["green0"] == ["1"], "nested capture data was lost")


def test_pending_write_and_config_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "config.json"
        config = WorkstationConfig(
            records_root=str(root / "records"),
            mes=MesConfig(device="DE0001", line="L1", response_success_field="res"),
        )
        save_config(config, config_path)
        loaded = load_config(config_path)
        _assert(loaded.mes.device == "DE0001", "MES config did not round-trip")
        pending = write_pending_request(
            loaded.records_root,
            run_id="RUN/001",
            url=loaded.mes.postxtdata_url,
            payload={"SN": "SN001", "run_id": "RUN/001"},
            operation="postxtdata",
            last_error="network unavailable",
        )
        document = json.loads(pending.read_text(encoding="utf-8"))
        _assert(pending.name == "RUN_001.json", "pending filename was not sanitized")
        _assert(document["payload"]["SN"] == "SN001", "pending payload was not persisted")
        _assert(not list(pending.parent.glob("*.tmp")), "temporary pending file was left behind")


def test_sample_truncation_is_explicit() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        record = RunRecord(Path(temporary) / "RUN_SAMPLE", "FULL", "SN001")
        record.start_step(1, "PPG reflect capture", PPG_REFLECT_CAPTURE_CMD)
        for index in range(MAX_STEP_SAMPLES + 1):
            record.ingest_line(
                f"+HW:PPG:F:{index},50,1,2,3,4,5,6,7,8,0x03"
            )
        record.ingest_line("+HW:PPG:CAPTURE:samples=1001,status=PASS")
        record.log_item(
            "FULL",
            "PPG reflect capture",
            PPG_REFLECT_CAPTURE_CMD,
            "PASS",
            1000,
            "",
            "+HW:PPG:CAPTURE:samples=1001,status=PASS ; OK",
        )
        data = build_run_data(
            record.run_summary(
                process_started_at="2026-07-20T14:00:00",
                process_ended_at="2026-07-20T14:01:00",
                device_result="PASS",
                device_message="completed",
            )
        )
        record.finish("PASS", "completed")

        measurements = data["test_items"]["ppg_reflect_capture"]["measurements"]
        _assert(measurements["sample_count"] == MAX_STEP_SAMPLES + 1, "actual sample count was lost")
        _assert(measurements["uploaded_sample_count"] == MAX_STEP_SAMPLES, "sample upload limit failed")
        _assert(measurements["truncated"], "sample truncation was not reported")
        _assert(len(measurements["samples"]["seq"]) == MAX_STEP_SAMPLES, "column sample length mismatch")


def test_sample_columns_preserve_index_with_missing_fields() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        record = RunRecord(Path(temporary) / "RUN_COLUMNS", "FULL", "SN001")
        record.start_step(1, "PPG reflect capture", PPG_REFLECT_CAPTURE_CMD)
        record.ingest_line("+HW:PPG:F:0,50,1,2,3,4,5,6,7,8,0x03")
        record.ingest_line("+HW:PPG:F:malformed")
        record.log_item("FULL", "PPG reflect capture", PPG_REFLECT_CAPTURE_CMD, "NG", 100, "parse", "parse")
        data = build_run_data(
            record.run_summary(
                process_started_at="2026-07-20T14:00:00",
                process_ended_at="2026-07-20T14:01:00",
                device_result="NG",
                device_message="parse",
            )
        )
        record.finish("NG", "parse")

        measurements = data["test_items"]["ppg_reflect_capture"]["measurements"]
        columns = measurements["samples"]
        _assert(measurements["uploaded_sample_count"] == 2, "column sample count mismatch")
        _assert(columns["seq"] == ["0", None], "missing seq was not aligned with null")
        _assert(columns["parse_error"][0] is None, "late column was not backfilled with null")
        _assert(all(len(values) == 2 for values in columns.values()), "column lengths diverged")


def test_cli_diagnostic_commands() -> None:
    with tempfile.TemporaryDirectory() as temporary, _Server() as server:
        root = Path(temporary)
        config_path = root / "config.json"
        config = WorkstationConfig(
            records_root=str(root / "records"),
            mes=_mes_config(server.base_url),
        )
        save_config(config, config_path)
        check_code = run_cli(
            [
                "mes-checkroute",
                "--config",
                str(config_path),
                "--sn",
                "SN001",
            ]
        )
        _assert(check_code == 0, f"mes-checkroute exit={check_code}")
        preview_code = run_cli(
            [
                "mes-post",
                "--config",
                str(config_path),
                "--sn",
                "SN001",
                "--mes-station",
                "full",
            ]
        )
        _assert(preview_code == 0, f"mes-post preview exit={preview_code}")
        post_code = run_cli(
            [
                "mes-post",
                "--config",
                str(config_path),
                "--sn",
                "SN001",
                "--mes-send",
            ]
        )
        _assert(post_code == 0, f"mes-post send exit={post_code}")


def _formal_cli(
    config_path: Path,
    flow: str,
    responses: dict[str, list[str]],
) -> tuple[int, list[str]]:
    transports: list[_ScriptedTransport] = []

    def factory(_config, _args, line_callback):
        transport = _ScriptedTransport(responses)
        transports.append(transport)
        return ATClient(transport, line_callback)

    code = run_cli(
        [
            flow,
            "--config",
            str(config_path),
            "--transport",
            "uart",
            "--port",
            "MOCK",
            "--sn",
            "SN001",
            "--token",
            "TOKEN",
            "--sn-record",
            "--no-flash-before-test",
        ],
        transport_factory=factory,
    )
    return code, [command for transport in transports for command in transport.commands]


def test_formal_half_and_full_upload_nested_results() -> None:
    with tempfile.TemporaryDirectory() as temporary, _Server() as server:
        root = Path(temporary)
        records_root = root / "records"
        config_path = root / "config.json"
        save_config(
            WorkstationConfig(
                records_root=str(records_root),
                mes=_mes_config(server.base_url),
            ),
            config_path,
        )

        half_code, half_commands = _formal_cli(config_path, "half", _half_responses())
        _assert(half_code == 0, f"formal half MES exit={half_code}")
        _assert("AT+HW=POWER" in half_commands, "half device flow did not run")
        _assert([item["path"] for item in _MesHandler.requests] == ["/postxtdata"], "half MES upload order failed")
        half_payload = _MesHandler.requests[-1]["payload"]
        half_items = half_payload["test_items"]
        _assert("Data" not in half_payload, "half payload retained legacy Data wrapper")
        _assert("Result" not in half_payload, "half payload retained legacy Result field")
        _assert(half_payload["device_result"] == "PASS", "half result was not PASS")
        _assert(half_payload["station_type"] == "HALF", "half station type was not promoted")
        _assert(half_items["power_path"]["result"] == "PASS", "half item result missing")
        _assert(half_items["power_path"]["response_summary"], "half item log summary missing")
        _assert(half_items["power_path"]["measurements"], "half item measurements missing")
        dark_measurements = half_items["ppg_dark_capture"]["measurements"]
        _assert(dark_measurements["sample_count"] == 1, "half PPG sample count missing")
        _assert(dark_measurements["uploaded_sample_count"] == 1, "half PPG sample was not uploaded")
        _assert(not dark_measurements["truncated"], "half PPG samples were unexpectedly truncated")
        _assert(
            dark_measurements["samples"]["green0"] == ["1"],
            "half PPG raw sample value missing",
        )
        _assert("Factory unlock" not in {item["name"] for item in half_items.values()}, "factory control leaked into MES items")

        half_log = next(records_root.rglob("*_HALF_SN001/unified_log.csv"))
        with half_log.open("r", encoding="utf-8", newline="") as file:
            event_types = [row["event_type"] for row in csv.DictReader(file)]
        _assert(event_types.index("mes_checkroute_skipped") < event_types.index("step_end"), "MES upload setup was not before device steps")
        _assert(event_types.index("mes_post_start") > event_types.index("step_end"), "MES result posted before device result")
        _assert(event_types.index("mes_post_end") < event_types.index("flow_end"), "record closed before MES result")

        full_code, full_commands = _formal_cli(config_path, "full", _full_responses())
        _assert(full_code == 0, f"formal full MES exit={full_code}")
        _assert(TOUCH_CAPTURE_CMD in full_commands, "full capture flow did not run")
        _assert(
            [item["path"] for item in _MesHandler.requests]
            == ["/postxtdata", "/postxtdata"],
            "full MES request order failed",
        )
        full_payload = _MesHandler.requests[-1]["payload"]
        full_items = full_payload["test_items"]
        _assert(full_payload["Station"] == "整机测试", "full MES station was incorrect")
        _assert(full_payload["station_type"] == "FULL", "full station type was not promoted")
        touch_measurements = full_items["touch_capture"]["measurements"]
        vib_measurements = full_items["lra_vibcapture"]["measurements"]
        ppg_measurements = full_items["ppg_reflect_capture"]["measurements"]
        _assert(touch_measurements["sample_count"] == 1, "touch raw sample was not uploaded")
        _assert(vib_measurements["sample_count"] == 1, "vibration raw sample was not uploaded")
        _assert(ppg_measurements["sample_count"] == 1, "PPG raw sample was not uploaded")
        _assert(touch_measurements["samples"]["raw0"] == ["100"], "touch sample fields missing")
        _assert(vib_measurements["samples"]["az"] == ["998"], "vibration sample fields missing")
        _assert(ppg_measurements["samples"]["green0"] == ["123"], "PPG sample fields missing")
        for measurements in (touch_measurements, vib_measurements, ppg_measurements):
            _assert(
                all(len(values) == measurements["uploaded_sample_count"] for values in measurements["samples"].values()),
                "sample columns are not index-aligned",
            )
        _assert(
            all(not item["measurements"]["truncated"] for item in (
                full_items["touch_capture"],
                full_items["lra_vibcapture"],
                full_items["ppg_reflect_capture"],
            )),
            "full-machine samples were unexpectedly truncated",
        )


def test_checkroute_rejects_before_device_access() -> None:
    with tempfile.TemporaryDirectory() as temporary, _Server({"/checkroute": False}) as server:
        root = Path(temporary)
        config_path = root / "config.json"
        config = WorkstationConfig(records_root=str(root / "records"), mes=_mes_config(server.base_url))
        config.mes.checkroute_enabled = True
        save_config(config, config_path)
        code, commands = _formal_cli(config_path, "half", _half_responses())
        _assert(code == 3, f"route rejection exit={code}")
        _assert(not commands, "device was accessed after MES route rejection")
        _assert([item["path"] for item in _MesHandler.requests] == ["/checkroute"], "post was sent after route rejection")


def test_post_reject_creates_pending_and_preserves_device_result() -> None:
    with tempfile.TemporaryDirectory() as temporary, _Server({"/postxtdata": False}) as server:
        root = Path(temporary)
        records_root = root / "records"
        config_path = root / "config.json"
        save_config(
            WorkstationConfig(records_root=str(records_root), mes=_mes_config(server.base_url)),
            config_path,
        )
        code, commands = _formal_cli(config_path, "half", _half_responses())
        _assert(code == 3, f"post rejection exit={code}")
        _assert("AT+HW=POWER" in commands, "device flow did not complete before post rejection")
        pending_files = list((records_root / "mes_pending").glob("*.json"))
        _assert(len(pending_files) == 1, "MES pending request was not persisted")
        pending = json.loads(pending_files[0].read_text(encoding="utf-8"))
        _assert(pending["payload"]["device_result"] == "PASS", "pending request lost the device PASS result")
        _assert(pending["payload"]["test_items"]["power_path"]["result"] == "PASS", "pending item result missing")


def test_device_failure_uploads_failed_item_log() -> None:
    with tempfile.TemporaryDirectory() as temporary, _Server() as server:
        root = Path(temporary)
        config_path = root / "config.json"
        save_config(
            WorkstationConfig(records_root=str(root / "records"), mes=_mes_config(server.base_url)),
            config_path,
        )
        responses = _half_responses()
        responses["AT+HW=POWER"] = ["+CME ERROR: 21,power_path_failed"]
        code, commands = _formal_cli(config_path, "half", responses)
        _assert(code == 2, f"device failure exit={code}")
        _assert("AT+HW=POWER" in commands, "failing device step did not run")
        payload = _MesHandler.requests[-1]["payload"]
        _assert(payload["device_result"] == "FAIL", "device failure was not uploaded as FAIL")
        power_item = payload["test_items"]["power_path"]
        _assert(power_item["result"] == "NG", "failed test item result was lost")
        _assert("power_path_failed" in power_item["error_reason"], "failed item error log was lost")
        failed_items = payload["failed_items"]
        _assert(
            failed_items.get("power_path", {}).get("item_key") == "power_path",
            "failed item index was not uploaded",
        )


def test_dry_run_skips_mes() -> None:
    with tempfile.TemporaryDirectory() as temporary, _Server() as server:
        root = Path(temporary)
        config_path = root / "config.json"
        save_config(
            WorkstationConfig(records_root=str(root / "records"), mes=_mes_config(server.base_url)),
            config_path,
        )
        transports: list[_ScriptedTransport] = []

        def factory(_config, _args, line_callback):
            transport = _ScriptedTransport(_half_responses())
            transports.append(transport)
            return ATClient(transport, line_callback)

        code = run_cli(
            [
                "half",
                "--config",
                str(config_path),
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--token",
                "TOKEN",
                "--no-sn-record",
                "--no-flash-before-test",
            ],
            transport_factory=factory,
        )
        _assert(code == 0, f"dry-run exit={code}")
        _assert(not _MesHandler.requests, "dry-run unexpectedly called MES")


def main() -> int:
    tests = [
        test_http_utf8_and_response_rule,
        test_unconfigured_business_rule_is_unknown,
        test_flat_post_payload_with_nested_items,
        test_pending_write_and_config_roundtrip,
        test_sample_truncation_is_explicit,
        test_sample_columns_preserve_index_with_missing_fields,
        test_cli_diagnostic_commands,
        test_formal_half_and_full_upload_nested_results,
        test_checkroute_rejects_before_device_access,
        test_post_reject_creates_pending_and_preserves_device_result,
        test_device_failure_uploads_failed_item_log,
        test_dry_run_skips_mes,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS MES smoke ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
