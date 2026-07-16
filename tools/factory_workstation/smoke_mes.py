from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factory_workstation.cli import run as run_cli
    from factory_workstation.config import MesConfig, WorkstationConfig, load_config, save_config
    from factory_workstation.mes_client import MesHttpClient
    from factory_workstation.mes_service import (
        MesService,
        build_postxtdata_payload,
        evaluate_response,
        write_pending_request,
    )
else:
    from .cli import run as run_cli
    from .config import MesConfig, WorkstationConfig, load_config, save_config
    from .mes_client import MesHttpClient
    from .mes_service import (
        MesService,
        build_postxtdata_payload,
        evaluate_response,
        write_pending_request,
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _MesHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append({"path": self.path, "payload": payload})
        body = json.dumps({"success": True, "message": "允许测试"}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return None


class _Server:
    def __enter__(self):
        _MesHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MesHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def _mes_config(base_url: str) -> MesConfig:
    return MesConfig(
        checkroute_url=f"{base_url}/checkroute",
        postxtdata_url=f"{base_url}/postxtdata",
        device="DE0001",
        line="L1",
        response_success_field="success",
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


def test_nested_post_payload() -> None:
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
                    "measurements": {"frames": [{"green0": 1, "red0": 2}]},
                    "logs": [{"direction": "RX", "line": "+HW:PPG:F:..."}],
                }
            },
        },
        ec_list=[{"ERROR_CODE": "M107", "LOCATION": "U10"}],
    )
    _assert(payload["Station"] == "整机测试", "full station mapping failed")
    frames = payload["Data"]["test_items"]["ppg_capture"]["measurements"]["frames"]
    _assert(frames[0]["green0"] == 1, "nested capture data was lost")


def test_pending_write_and_config_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "config.json"
        config = WorkstationConfig(
            records_root=str(root / "records"),
            mes=MesConfig(device="DE0001", line="L1", response_success_field="success"),
        )
        save_config(config, config_path)
        loaded = load_config(config_path)
        _assert(loaded.mes.device == "DE0001", "MES config did not round-trip")
        pending = write_pending_request(
            loaded.records_root,
            run_id="RUN/001",
            url=loaded.mes.postxtdata_url,
            payload={"SN": "SN001", "Data": {"run_id": "RUN/001"}},
            operation="postxtdata",
            last_error="network unavailable",
        )
        document = json.loads(pending.read_text(encoding="utf-8"))
        _assert(pending.name == "RUN_001.json", "pending filename was not sanitized")
        _assert(document["payload"]["SN"] == "SN001", "pending payload was not persisted")
        _assert(not list(pending.parent.glob("*.tmp")), "temporary pending file was left behind")


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


def main() -> int:
    tests = [
        test_http_utf8_and_response_rule,
        test_unconfigured_business_rule_is_unknown,
        test_nested_post_payload,
        test_pending_write_and_config_roundtrip,
        test_cli_diagnostic_commands,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS MES smoke ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
