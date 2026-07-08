from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import warnings
from pathlib import Path
from typing import Callable


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factory_workstation import cli
    from factory_workstation import flash_flow
    from factory_workstation import flash_runner
    from factory_workstation import flows
    from factory_workstation.at_client import ATClient, CommandResult
    from factory_workstation.at_parser import is_capture_frame_line, parse_line
    from factory_workstation.config import WorkstationConfig, get_factory_token, redact_sensitive_text, save_factory_token, verify_engineer_password
    from factory_workstation.flash_runner import FlashCommand, FlashOutcome
    from factory_workstation import ota_runner
    from factory_workstation import transport_ble
    from factory_workstation.storage import RunStorage
else:
    from . import cli
    from . import flash_flow
    from . import flash_runner
    from . import flows
    from .at_client import ATClient, CommandResult
    from .at_parser import is_capture_frame_line, parse_line
    from .config import WorkstationConfig, get_factory_token, redact_sensitive_text, save_factory_token, verify_engineer_password
    from .flash_runner import FlashCommand, FlashOutcome
    from . import ota_runner
    from . import transport_ble
    from .storage import RunStorage


Progress = Callable[[int, str, str, str], None]


HALF_CHIP_COMMUNICATION_COMMANDS = [
    "AT+HW=IMU,PROBE",
    "AT+HW=TOUCH,PROBE",
    "AT+HW=CHG,REGS",
    "AT+HW=GAUGE,DATA",
    "AT+HW=FLASH,PROBE",
    "AT+HW=PPG,PROBE",
]

TOUCH_CAPTURE_CMD = "AT+HW=TOUCH,CAPTURE,CONFIRM,3000,COMPACT"
VIB_CAPTURE_CMD = "AT+HW=IMU,VIBCAPTURE,CONFIRM,50,3000,20,COMPACT"
PPG_REFLECT_CAPTURE_CMD = "AT+HW=PPG,CAPTURE,CONFIRM,REFLECT,3000,50,COMPACT"
PPG_DARK_CAPTURE_CMD = "AT+HW=PPG,CAPTURE,CONFIRM,DARK,1000,100,COMPACT"
LEGACY_TOUCH_CAPTURE_CMD = "AT+HW=TOUCH,CAPTURE,CONFIRM,3000"
LEGACY_VIB_CAPTURE_CMD = "AT+HW=IMU,VIBCAPTURE,CONFIRM,50,3000,20"
LEGACY_PPG_REFLECT_CAPTURE_CMD = "AT+HW=PPG,CAPTURE,CONFIRM,REFLECT,3000,50"
LEGACY_PPG_DARK_CAPTURE_CMD = "AT+HW=PPG,CAPTURE,CONFIRM,DARK,1000,100"


class FakeRecord:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str, str]] = []
        self.finished: tuple[str, str] | None = None
        self.at_lines: list[tuple[str, str]] = []
        self.steps: list[tuple[int, str, str]] = []

    def start_step(self, step_index: int, step_name: str, command: str) -> None:
        self.steps.append((step_index, step_name, command))

    def log_event(self, event_type: str, payload: dict | None = None, raw_line: str = "") -> None:
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
        self.items.append((station_type, item_name, command, result, error_reason))

    def finish(self, result: str, details: str = "") -> None:
        self.finished = (result, details)

    def log_at(self, direction: str, line: str) -> None:
        self.at_lines.append((direction, line))


class ScriptedTransport:
    created: list["ScriptedTransport"] = []

    def __init__(
        self,
        name: str = "AXI-P1-T",
        address: str = "11:22:33:44:55:66",
        scan_timeout_s: float = 0.1,
        responses: dict[str, list[str]] | None = None,
    ) -> None:
        self.name = name
        self.address = address
        self.scan_timeout_s = scan_timeout_s
        self.responses = responses or {}
        self.commands: list[str] = []
        self.closed = False
        self._pending: list[str] = []
        ScriptedTransport.created.append(self)

    def write_line(self, command: str) -> None:
        command = command.strip()
        self.commands.append(command)
        self._pending = list(self.responses.get(command, ["OK"]))

    def read_line(self, timeout_s: float) -> str | None:
        if self._pending:
            return self._pending.pop(0)
        return None

    def clear_input(self) -> None:
        self._pending.clear()

    def close(self) -> None:
        self.closed = True


class FakeBleTransport(ScriptedTransport):
    default_responses = {
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=2.1.0,build=smoke", "OK"],
        "AT+SN?": ["+SN:value=SN001,source=lfs", "OK"],
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT+OTABUSY?": ["+OTABUSY:locked=0", "OK"],
        "AT+OTA?": ["+OTA:locked=0,state=idle", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        TOUCH_CAPTURE_CMD: ["+HW:TOUCH:FRAME:0,0,0x02,0x00000000,0x00000001,100/101/102/103,1/2/3/4,90/91/92/93,80/81/82/83", "+HW:TOUCH:CAPTURE:samples=60", "OK"],
        VIB_CAPTURE_CMD: ["+HW:IMU:VIBF:0,20,12,-3,998,1,0,-1,50", "+HW:IMU:VIBSUMMARY:samples=150,duration_ms=3000,interval_ms=20,elapsed_ms=3020,overruns=0,max_late_ms=0,status=PASS", "OK"],
        PPG_REFLECT_CAPTURE_CMD: ["+HW:PPG:F:0,50,123,456,789,124,457,790,10,11,0x03", "+HW:PPG:CAPTURE:samples=60,status=PASS", "OK"],
        "AT+FACTORY=LOCK": ["OK"],
    }

    def __init__(self, name: str = "AXI-P1-T", address: str = "", scan_timeout_s: float = 0.1) -> None:
        super().__init__(name, address or "11:22:33:44:55:66", scan_timeout_s, dict(self.default_responses))


class TimeoutRecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def send_command(self, command: str, timeout_s: float):
        self.calls.append((command, timeout_s))
        responses = {
            "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
            "AT+VER?": ["+VER:version=2.1.0,build=smoke", "OK"],
            "AT+OTABUSY?": ["+OTABUSY:locked=0", "OK"],
            TOUCH_CAPTURE_CMD: ["+HW:TOUCH:CAPTURE:samples=60", "OK"],
            VIB_CAPTURE_CMD: ["+HW:IMU:VIBSUMMARY:samples=150,status=PASS", "OK"],
            PPG_REFLECT_CAPTURE_CMD: ["+HW:PPG:CAPTURE:samples=60,status=PASS", "OK"],
            LEGACY_TOUCH_CAPTURE_CMD: ["+HW:TOUCH:CAPTURE:samples=60", "OK"],
            LEGACY_VIB_CAPTURE_CMD: ["+HW:IMU:VIBSUMMARY:samples=150,status=PASS", "OK"],
            LEGACY_PPG_REFLECT_CAPTURE_CMD: ["+HW:PPG:CAPTURE:samples=60,status=PASS", "OK"],
        }
        return CommandResult(command, True, responses.get(command, ["OK"]), elapsed_s=0.0)


def _progress(_index: int, _label: str, _status: str, _detail: str) -> None:
    return None


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_sensitive_factory_tokens_are_redacted() -> None:
    secret = "SECRET_TOKEN"
    unlock_command = f"AT+FACTORY=UNLOCK,{secret}"
    redacted_command = "AT+FACTORY=UNLOCK,***"

    _assert(redact_sensitive_text(unlock_command) == redacted_command, "unlock token was not redacted")
    _assert(
        redact_sensitive_text(f"AT+FACTORY=RECOVER,{secret}") == "AT+FACTORY=RECOVER,***",
        "recover token was not redacted",
    )

    seen: list[tuple[str, str]] = []
    transport = ScriptedTransport(responses={unlock_command: ["OK"]})
    result = ATClient(transport, lambda direction, line: seen.append((direction, line))).send_command(
        unlock_command,
        1.0,
    )
    _assert(result.ok, "unlock command did not complete in redaction smoke")
    _assert(transport.commands == [unlock_command], "redaction changed the command sent to device")
    _assert(("TX", redacted_command) in seen, "TX callback did not redact token")
    _assert(secret not in repr(seen), "TX callback leaked token")

    with tempfile.TemporaryDirectory() as tmp:
        record = RunStorage(tmp).start_run("HALF", "SN001", "")
        run_dir = record.run_dir
        record.log_at("TX", unlock_command)
        record.log_item("half", "Factory unlock", unlock_command, "PASS", 1, "", "")
        record.finish("PASS", "completed")
        unified_csv = (run_dir / "unified_log.csv").read_text(encoding="utf-8")
        _assert(secret not in unified_csv, "unified_log.csv leaked token")
        _assert(redacted_command in unified_csv, "unified_log.csv did not keep redacted command")
        _assert({path.name for path in run_dir.iterdir() if path.is_file()} == {"unified_log.csv"}, "SN record wrote extra files")

    responses = _half_success_responses()
    responses[unlock_command] = responses.pop("AT+FACTORY=UNLOCK,TOKEN")
    with tempfile.TemporaryDirectory() as tmp:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code, commands = _run_cli_with_responses(
                [
                    "half",
                    "--transport",
                    "uart",
                    "--port",
                    "MOCK",
                    "--records-root",
                    tmp,
                    "--sn",
                    "SN001",
                    "--token",
                    secret,
                    "--sn-record",
                    "--skip-momo",
                ],
                responses,
            )
        output = buf.getvalue()
        _assert(exit_code == 0, f"CLI redaction smoke exit={exit_code}")
        _assert(unlock_command in commands, "CLI did not send actual unlock command")
        _assert(secret not in output, "CLI stdout leaked token")
        _assert(redacted_command in output, "CLI stdout did not show redacted unlock command")


def test_engineer_password_and_saved_token() -> None:
    old_plain = os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD")
    old_hash = os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD_SHA256")
    old_token = os.environ.get("AXI_FACTORY_ENGINEER_TOKEN")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from factory_workstation import config as config_module

            original_env_path = config_module.ENV_PATH
            config_module.ENV_PATH = Path(tmp) / ".env"
            try:
                config_module.ENV_PATH.write_text(
                    "AXI_FACTORY_ENGINEER_PASSWORD=engineer-pass\n",
                    encoding="utf-8",
                )
                os.environ["AXI_FACTORY_ENGINEER_PASSWORD"] = "stale-password"
                os.environ.pop("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", None)
                _assert(verify_engineer_password("engineer-pass", WorkstationConfig()), "engineer password was rejected")
                _assert(not verify_engineer_password("wrong", WorkstationConfig()), "wrong engineer password was accepted")
                save_factory_token("TOKEN_FROM_ENGINEER")
                _assert(os.environ.get("AXI_FACTORY_ENGINEER_TOKEN") == "TOKEN_FROM_ENGINEER", "saved token not in env")
                _assert("TOKEN_FROM_ENGINEER" in config_module.ENV_PATH.read_text(encoding="utf-8"), "saved token not in .env")
                os.environ["AXI_FACTORY_ENGINEER_TOKEN"] = "STALE_TOKEN_FROM_SHELL"
                _assert(get_factory_token("") == "TOKEN_FROM_ENGINEER", ".env token did not override stale env token")
            finally:
                config_module.ENV_PATH = original_env_path
    finally:
        if old_plain is None:
            os.environ.pop("AXI_FACTORY_ENGINEER_PASSWORD", None)
        else:
            os.environ["AXI_FACTORY_ENGINEER_PASSWORD"] = old_plain
        if old_hash is None:
            os.environ.pop("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", None)
        else:
            os.environ["AXI_FACTORY_ENGINEER_PASSWORD_SHA256"] = old_hash
        if old_token is None:
            os.environ.pop("AXI_FACTORY_ENGINEER_TOKEN", None)
        else:
            os.environ["AXI_FACTORY_ENGINEER_TOKEN"] = old_token


def _half_success_responses() -> dict[str, list[str]]:
    return {
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=2.1.0,build=smoke", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        "AT+SN=SN001": ["OK"],
        "AT+SN?": ["+SN:value=SN001,source=lfs", "OK"],
        "AT+HW=POWER": ["+HW:POWER:status=PASS", "OK"],
        "AT+HW=IMU,PROBE": ["+HW:IMU:PROBE:status=PASS", "OK"],
        "AT+HW=TOUCH,PROBE": ["+HW:TOUCH:PROBE:status=PASS", "OK"],
        "AT+HW=CHG,REGS": ["+HW:CHG:REGS:status=PASS", "OK"],
        "AT+HW=GAUGE,DATA": ["+HW:GAUGE:DATA:status=PASS", "OK"],
        "AT+HW=FLASH,PROBE": ["+HW:FLASH:PROBE:status=PASS", "OK"],
        "AT+HW=PPG,PROBE": ["+HW:PPG:PROBE:status=PASS", "OK"],
        PPG_DARK_CAPTURE_CMD: ["+HW:PPG:F:0,50,1,2,3,4,5,6,7,8,0x03", "+HW:PPG:CAPTURE:samples=10,status=PASS", "OK"],
        LEGACY_PPG_DARK_CAPTURE_CMD: ["+HW:PPG:CAPTURE:samples=10,status=PASS", "OK"],
        "AT+HW=TOUCH,ISR,CONFIRM": ["+HW:TOUCH:ISR:status=PASS", "OK"],
        "AT+FACTORY=LOCK": ["OK"],
    }


def _full_success_responses() -> dict[str, list[str]]:
    return {
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=2.1.0,build=smoke", "OK"],
        "AT+SN?": ["+SN:value=SN001,source=lfs", "OK"],
        "AT+OTABUSY?": ["+OTABUSY:locked=0", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        TOUCH_CAPTURE_CMD: ["+HW:TOUCH:FRAME:0,0,0x02,0x00000000,0x00000001,100/101/102/103,1/2/3/4,90/91/92/93,80/81/82/83", "+HW:TOUCH:CAPTURE:samples=60", "OK"],
        VIB_CAPTURE_CMD: ["+HW:IMU:VIBF:0,20,12,-3,998,1,0,-1,50", "+HW:IMU:VIBSUMMARY:samples=150,duration_ms=3000,interval_ms=20,elapsed_ms=3020,overruns=0,max_late_ms=0,status=PASS", "OK"],
        PPG_REFLECT_CAPTURE_CMD: ["+HW:PPG:F:0,50,123,456,789,124,457,790,10,11,0x03", "+HW:PPG:CAPTURE:samples=60,status=PASS", "OK"],
        LEGACY_TOUCH_CAPTURE_CMD: ["+HW:TOUCH:CAPTURE:samples=60", "OK"],
        LEGACY_VIB_CAPTURE_CMD: ["+HW:IMU:VIBSUMMARY:samples=150,status=PASS", "OK"],
        LEGACY_PPG_REFLECT_CAPTURE_CMD: ["+HW:PPG:CAPTURE:samples=60,status=PASS", "OK"],
        "AT+FACTORY=LOCK": ["OK"],
    }


def test_unlock_failure_cleanup() -> None:
    responses = {
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=2.1.0,build=smoke", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        "AT+SN=SN001": ["OK"],
        "AT+SN?": ["+SN:value=SN001,source=lfs", "OK"],
        "AT+HW=POWER": ["+HW:POWER:status=PASS", "OK"],
        "AT+HW=IMU,PROBE": ["+HW:IMU:PROBE:status=PASS", "OK"],
        "AT+HW=TOUCH,PROBE": ["+HW:TOUCH:PROBE:status=PASS", "OK"],
        "AT+HW=CHG,REGS": ["+HW:CHG:REGS:status=PASS", "OK"],
        "AT+HW=GAUGE,DATA": ["+HW:GAUGE:DATA:status=PASS", "OK"],
        "AT+HW=FLASH,PROBE": ["+HW:FLASH:PROBE:status=PASS", "OK"],
        "AT+HW=PPG,PROBE": ["+HW:PPG:PROBE:status=PASS", "OK"],
        PPG_DARK_CAPTURE_CMD: ["+HW:PPG:CAPTURE:samples=10,status=PASS", "OK"],
        "AT+HW=TOUCH,ISR,CONFIRM": ["+CME ERROR: 12,touch_isr_timeout"],
        "AT+FACTORY=LOCK": ["OK"],
    }
    transport = ScriptedTransport(responses=responses)
    record = FakeRecord()
    outcome = flows.run_half_machine(
        ATClient(transport),
        WorkstationConfig(factory_at_required=True),
        "SN001",
        "TOKEN",
        record,  # type: ignore[arg-type]
        _progress,
    )

    _assert(outcome.result == "NG", f"expected NG, got {outcome.result}")
    _assert("AT+FACTORY=LOCK" in transport.commands, "cleanup lock command was not sent")
    cleanup_items = [item for item in record.items if item[1] == "Factory lock cleanup"]
    _assert(cleanup_items and cleanup_items[-1][3] == "PASS", "cleanup lock item was not PASS")


def test_sn_disabled_half_flow_skips_sn_commands() -> None:
    responses = _half_success_responses()
    config = WorkstationConfig(factory_at_required=True)
    config.sn_rule.min_len = 99
    transport = ScriptedTransport(responses=responses)
    outcome = flows.run_half_machine(
        ATClient(transport),
        config,
        "",
        "TOKEN",
        FakeRecord(),  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(outcome.result == "PASS", f"expected PASS, got {outcome.result}")
    _assert(not any(cmd.startswith("AT+SN=") for cmd in transport.commands), "SN disabled flow wrote SN")
    _assert("AT+SN?" not in transport.commands, "SN disabled flow read SN")


def test_sn_disabled_half_without_token_skips_factory_gate() -> None:
    config = WorkstationConfig(factory_at_required=True)
    config.sn_rule.min_len = 99
    transport = ScriptedTransport(responses=_half_success_responses())
    outcome = flows.run_half_machine(
        ATClient(transport),
        config,
        "",
        "",
        FakeRecord(),  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(outcome.result == "PASS", f"expected PASS, got {outcome.result}")
    _assert(not any(cmd.startswith("AT+FACTORY=") for cmd in transport.commands), "dry-run no-token flow used factory gate")
    _assert(not any(cmd.startswith("AT+SN") for cmd in transport.commands), "dry-run no-token flow used SN command")


def test_sn_enabled_missing_token_still_fails() -> None:
    transport = ScriptedTransport(responses=_half_success_responses())
    record = FakeRecord()
    outcome = flows.run_half_machine(
        ATClient(transport),
        WorkstationConfig(factory_at_required=True),
        "SN001",
        "",
        record,  # type: ignore[arg-type]
        _progress,
        sn_enabled=True,
    )

    _assert(outcome.result == "NG", f"expected NG, got {outcome.result}")
    _assert(outcome.message == "missing factory token", f"unexpected message: {outcome.message}")
    _assert(not transport.commands, "formal mode sent commands after missing token")
    _assert(record.finished == ("NG", "missing factory token"), "record did not finish missing token")


def test_sn_persistence_failure_marks_half_ng() -> None:
    responses = _half_success_responses()
    responses["AT+SN?"] = ["+SN:value=SN001,source=ram", "OK"]
    transport = ScriptedTransport(responses=responses)
    record = FakeRecord()
    outcome = flows.run_half_machine(
        ATClient(transport),
        WorkstationConfig(factory_at_required=True),
        "SN001",
        "TOKEN",
        record,  # type: ignore[arg-type]
        _progress,
        sn_enabled=True,
    )

    _assert(outcome.result == "NG", f"expected NG from SN persistence failure, got {outcome.result}")
    _assert(outcome.message == "SN persistence check failed", f"unexpected SN persistence message: {outcome.message}")
    _assert(record.finished == ("NG", "SN persistence check failed"), "record finish did not fail SN persistence")
    sn_items = [item for item in record.items if item[1] == "SN persistence check"]
    _assert(sn_items and sn_items[-1][3] == "NG", "SN persistence item was not NG")


def test_capability_step_numbering_and_single_cap_query() -> None:
    half_record = FakeRecord()
    half_transport = ScriptedTransport(responses=_half_success_responses())
    half_outcome = flows.run_half_machine(
        ATClient(half_transport),
        WorkstationConfig(factory_at_required=True),
        "",
        "",
        half_record,  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(half_outcome.result == "PASS", f"half flow expected PASS, got {half_outcome.result}")
    _assert(half_transport.commands.count("AT+CAP?") == 1, "half flow sent duplicate AT+CAP?")
    _assert(half_record.steps[0] == (1, "Factory AT capability", "AT+CAP?"), "half capability step is not step 1")
    _assert(half_record.steps[1][0] == 2 and half_record.steps[1][1] == "AT probe", "half AT probe numbering is wrong")

    full_record = FakeRecord()
    full_transport = ScriptedTransport(responses=_full_success_responses())
    full_outcome = flows.run_full_machine(
        ATClient(full_transport),
        WorkstationConfig(factory_at_required=True, ota_enabled=False),
        "",
        "",
        full_record,  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(full_outcome.result == "PASS", f"full flow expected PASS, got {full_outcome.result}")
    _assert(full_transport.commands.count("AT+CAP?") == 1, "full flow sent duplicate AT+CAP?")
    _assert(full_record.steps[0] == (1, "Factory AT capability", "AT+CAP?"), "full capability step is not step 1")


def test_sn_disabled_full_without_token_skips_factory_gate() -> None:
    config = WorkstationConfig(factory_at_required=True, ota_enabled=False)
    config.sn_rule.min_len = 99
    transport = ScriptedTransport(responses=_full_success_responses())
    outcome = flows.run_full_machine(
        ATClient(transport),
        config,
        "",
        "",
        FakeRecord(),  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(outcome.result == "PASS", f"expected PASS, got {outcome.result}")
    _assert(not any(cmd.startswith("AT+FACTORY=") for cmd in transport.commands), "dry-run no-token full flow used factory gate")
    _assert("AT+SN?" not in transport.commands, "dry-run no-token full flow read SN")


def test_full_capture_timeouts_have_minimums() -> None:
    config = WorkstationConfig(factory_at_required=True, ota_enabled=False)
    config.at_timeouts.touch_capture_s = 1.0
    config.at_timeouts.vibcapture_s = 1.0
    config.at_timeouts.ppg_capture_s = 1.0
    client = TimeoutRecordingClient()
    outcome = flows.run_full_machine(
        client,  # type: ignore[arg-type]
        config,
        "",
        "",
        FakeRecord(),  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(outcome.result == "PASS", f"expected PASS, got {outcome.result}")
    touch_calls = [timeout for command, timeout in client.calls if command == TOUCH_CAPTURE_CMD]
    _assert(touch_calls, "full flow did not run Touch capture")
    _assert(touch_calls[-1] >= 45.0, f"Touch capture timeout too short: {touch_calls[-1]}")
    vib_calls = [timeout for command, timeout in client.calls if command == VIB_CAPTURE_CMD]
    _assert(vib_calls, "full flow did not run LRA vibcapture")
    _assert(vib_calls[-1] >= 60.0, f"LRA vibcapture timeout too short: {vib_calls[-1]}")
    ppg_calls = [timeout for command, timeout in client.calls if command == PPG_REFLECT_CAPTURE_CMD]
    _assert(ppg_calls, "full flow did not run PPG capture")
    _assert(ppg_calls[-1] >= 45.0, f"PPG capture timeout too short: {ppg_calls[-1]}")


def test_capture_output_mode_legacy_fallback() -> None:
    config = WorkstationConfig(factory_at_required=True, ota_enabled=False)
    config.capture_output_mode = "legacy"
    transport = ScriptedTransport(responses=_full_success_responses())
    outcome = flows.run_full_machine(
        ATClient(transport),
        config,
        "",
        "",
        FakeRecord(),  # type: ignore[arg-type]
        _progress,
        sn_enabled=False,
    )

    _assert(outcome.result == "PASS", f"legacy flow expected PASS, got {outcome.result}")
    _assert(LEGACY_TOUCH_CAPTURE_CMD in transport.commands, "legacy flow did not use legacy Touch command")
    _assert(LEGACY_VIB_CAPTURE_CMD in transport.commands, "legacy flow did not use legacy LRA command")
    _assert(LEGACY_PPG_REFLECT_CAPTURE_CMD in transport.commands, "legacy flow did not use legacy PPG command")
    _assert(TOUCH_CAPTURE_CMD not in transport.commands, "legacy flow leaked compact Touch command")


def test_compact_parser_and_unified_log() -> None:
    touch = parse_line("+HW:TOUCH:FRAME:0,0,0x02,0x00000000,0x00000001,100/101/102/103,1/2/3/4,90/91/92/93,80/81/82/83")
    vib = parse_line("+HW:IMU:VIBF:1,20,12,-3,998,1,0,-1,50")
    ppg = parse_line("+HW:PPG:F:2,50,123,456,789,124,457,790,10,11,0x03")
    _assert(touch.kind == "touch_frame", f"touch compact parsed as {touch.kind}")
    _assert(touch.fields["raw0"] == "100" and touch.fields["baseline3"] == "83", "touch compact fields incomplete")
    _assert(vib.kind == "vib_frame" and vib.fields["gx"] == "1", "vib compact fields incomplete")
    _assert(ppg.kind == "ppg_frame" and ppg.fields["green0"] == "123", "ppg compact fields incomplete")
    _assert(is_capture_frame_line(ppg.line), "ppg compact was not detected as frame line")

    with tempfile.TemporaryDirectory() as tmp:
        record = RunStorage(tmp).start_run("FULL", "SN001", "")
        run_dir = record.run_dir
        record.start_step(1, "Factory unlock", "AT+FACTORY=UNLOCK,SECRET_TOKEN")
        record.log_at("TX", "AT+FACTORY=UNLOCK,SECRET_TOKEN")
        record.start_step(2, "Touch capture", TOUCH_CAPTURE_CMD)
        record.log_at("RX", touch.line)
        record.log_at("RX", vib.line)
        record.log_at("RX", ppg.line)
        record.log_at("RX", "+HW:IMU:VIBSUMMARY:samples=150,duration_ms=3000,interval_ms=20,elapsed_ms=3020,overruns=0,max_late_ms=0,status=PASS")
        record.log_item("full", "Touch capture", TOUCH_CAPTURE_CMD, "PASS", 1234, "", "OK")
        record.finish("PASS", "completed")

        unified_path = run_dir / "unified_log.csv"
        rows = list(csv.DictReader(unified_path.open("r", encoding="utf-8")))
        event_types = {row["event_type"] for row in rows}
        _assert({"flow_start", "step_start", "at_tx", "at_rx", "touch_frame", "vib_frame", "ppg_frame", "vib_summary", "step_end", "flow_end"} <= event_types, "unified_log missing events")
        unified_text = unified_path.read_text(encoding="utf-8")
        _assert("SECRET_TOKEN" not in unified_text, "unified_log leaked token")
        _assert("AT+FACTORY=UNLOCK,***" in unified_text, "unified_log did not retain redacted token")
        _assert({path.name for path in run_dir.iterdir() if path.is_file()} == {"unified_log.csv"}, "capture run wrote extra files")


def test_split_record_output_writes_compatibility_files() -> None:
    touch_line = "+HW:TOUCH:FRAME:0,0,0x02,0x00000000,0x00000001,100/101/102/103,1/2/3/4,90/91/92/93,80/81/82/83"
    vib_line = "+HW:IMU:VIBF:1,20,12,-3,998,1,0,-1,50"
    ppg_line = "+HW:PPG:F:2,50,123,456,789,124,457,790,10,11,0x03"
    summary_line = "+HW:IMU:VIBSUMMARY:samples=150,duration_ms=3000,interval_ms=20,elapsed_ms=3020,overruns=0,max_late_ms=0,status=PASS"
    secret = "SECRET_TOKEN"

    with tempfile.TemporaryDirectory() as tmp:
        record = RunStorage(tmp, write_extra_files=True).start_run("FULL", "SN001", "")
        run_dir = record.run_dir
        record.start_step(1, "Factory unlock", f"AT+FACTORY=UNLOCK,{secret}")
        record.log_at("TX", f"AT+FACTORY=UNLOCK,{secret}")
        record.log_item("full", "Factory unlock", f"AT+FACTORY=UNLOCK,{secret}", "PASS", 1, "", "OK")
        record.start_step(2, "Touch capture", TOUCH_CAPTURE_CMD)
        record.log_at("RX", touch_line)
        record.log_at("RX", vib_line)
        record.log_at("RX", ppg_line)
        record.log_at("RX", summary_line)
        record.log_item("full", "Touch capture", TOUCH_CAPTURE_CMD, "PASS", 1234, "", "OK")
        record.finish("PASS", "completed")

        expected_files = {
            "unified_log.csv",
            "raw_at.log",
            "factory_test_items.csv",
            "momo_raw.csv",
            "momo_filt.csv",
            "lra_frames.csv",
            "ppg_frames.csv",
            "capture_summary.csv",
            "metadata.json",
            "summary.csv",
        }
        actual_files = {path.name for path in run_dir.iterdir() if path.is_file()}
        _assert(expected_files <= actual_files, f"split record output missing files: {expected_files - actual_files}")
        for file_name in ("unified_log.csv", "raw_at.log", "factory_test_items.csv"):
            text = (run_dir / file_name).read_text(encoding="utf-8")
            _assert(secret not in text, f"{file_name} leaked token")
            _assert("AT+FACTORY=UNLOCK,***" in text, f"{file_name} did not keep redacted command")


def _fake_flash_outcome(ok: bool = True) -> FlashOutcome:
    command = FlashCommand(
        backend="nrfjprog",
        argv=["nrfjprog", "--program", "mock.hex", "--chiperase", "--verify", "--reset"],
        cwd=".",
        image_path="mock.hex",
        image_sha256="a" * 64,
        jlink_probe_id="MOCKJLINK",
        env={},
    )
    return FlashOutcome(
        ok=ok,
        result="PASS" if ok else "NG",
        message="mock flash completed" if ok else "mock flash failed",
        elapsed_ms=123,
        exit_code=0 if ok else 1,
        command=command,
    )


def _run_cli_with_responses(
    args: list[str],
    responses: dict[str, list[str]],
    fake_flash_ok: bool | None = None,
    flash_calls_out: list[WorkstationConfig] | None = None,
) -> tuple[int, list[str]]:
    ScriptedTransport.created.clear()

    def factory(
        _config: WorkstationConfig,
        _args,
        line_callback,
    ) -> ATClient:
        return ATClient(ScriptedTransport(responses=responses), line_callback)

    flash_calls: list[WorkstationConfig] = []

    def fake_flash(config: WorkstationConfig, line_callback) -> FlashOutcome:
        flash_calls.append(config)
        if line_callback is not None:
            line_callback("FLASH", "mock flash line")
        return _fake_flash_outcome(fake_flash_ok is not False)

    exit_code = cli.run(
        args,
        transport_factory=factory,
        flash_runner=fake_flash if fake_flash_ok is not None else None,
    )
    commands = [cmd for transport in ScriptedTransport.created for cmd in transport.commands]
    if flash_calls_out is not None:
        flash_calls_out.extend(flash_calls)
    if fake_flash_ok is not None and not flash_calls:
        raise AssertionError("fake flash runner was not called")
    return exit_code, commands


def test_cli_half_full_sn_modes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        exit_code, half_sn_commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
            ],
            _half_success_responses(),
        )
        _assert(exit_code == 0, f"CLI half SN exit={exit_code}")
        _assert("AT+SN=SN001" in half_sn_commands, "CLI half SN did not write SN")
        for command in HALF_CHIP_COMMUNICATION_COMMANDS:
            _assert(command in half_sn_commands, f"CLI half SN missed chip communication command: {command}")
        _assert(any(Path(tmp).glob("*/*/unified_log.csv")), "CLI half SN did not write unified_log.csv")
        _assert(not any(path.name != "unified_log.csv" for path in Path(tmp).glob("*/*/*")), "CLI half SN wrote extra record files")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, _half_split_commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--record-output-mode",
                "split",
            ],
            _half_success_responses(),
        )
        _assert(exit_code == 0, f"CLI half split record exit={exit_code}")
        _assert(any(Path(tmp).glob("*/*/unified_log.csv")), "CLI half split did not write unified_log.csv")
        _assert(any(Path(tmp).glob("*/*/summary.csv")), "CLI half split did not write summary.csv")
        _assert(any(Path(tmp).glob("*/*/factory_test_items.csv")), "CLI half split did not write factory_test_items.csv")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, half_dry_commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--no-sn-record",
            ],
            _half_success_responses(),
        )
        _assert(exit_code == 0, f"CLI half dry-run exit={exit_code}")
        _assert(not list(Path(tmp).glob("*")), "CLI half dry-run wrote records")
        _assert(not any(cmd.startswith("AT+SN") for cmd in half_dry_commands), "CLI half dry-run used SN command")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, full_sn_commands = _run_cli_with_responses(
            [
                "full",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--no-ota",
            ],
            _full_success_responses(),
        )
        _assert(exit_code == 0, f"CLI full SN exit={exit_code}")
        _assert("AT+SN?" in full_sn_commands, "CLI full SN did not read SN")
        _assert(any(cmd.startswith("AT+FACTORY=UNLOCK") for cmd in full_sn_commands), "CLI full SN did not unlock factory")
        _assert(TOUCH_CAPTURE_CMD in full_sn_commands, "CLI full SN did not use compact Touch capture")
        _assert(VIB_CAPTURE_CMD in full_sn_commands, "CLI full SN did not use compact LRA capture")
        _assert(PPG_REFLECT_CAPTURE_CMD in full_sn_commands, "CLI full SN did not use compact PPG capture")
        _assert(any(Path(tmp).glob("*/*/unified_log.csv")), "CLI full SN did not write unified_log.csv")
        _assert(not any(path.name != "unified_log.csv" for path in Path(tmp).glob("*/*/*")), "CLI full SN wrote extra record files")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, full_dry_commands = _run_cli_with_responses(
            [
                "full",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--no-sn-record",
                "--no-ota",
            ],
            _full_success_responses(),
        )
        _assert(exit_code == 0, f"CLI full dry-run exit={exit_code}")
        _assert("AT+SN?" not in full_dry_commands, "CLI full dry-run read SN")
        _assert(not list(Path(tmp).glob("*")), "CLI full dry-run wrote records")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, half_skip_momo_commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--skip-momo",
            ],
            _half_success_responses(),
        )
        _assert(exit_code == 0, f"CLI half skip-momo exit={exit_code}")
        _assert("AT+HW=TOUCH,PROBE" in half_skip_momo_commands, "CLI half skip-momo skipped Touch communication")
        _assert("AT+HW=TOUCH,ISR,CONFIRM" not in half_skip_momo_commands, "CLI half skip-momo ran Touch ISR")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, full_skip_momo_commands = _run_cli_with_responses(
            [
                "full",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--no-ota",
                "--skip-momo",
            ],
            _full_success_responses(),
        )
        _assert(exit_code == 0, f"CLI full skip-momo exit={exit_code}")
        _assert(
            TOUCH_CAPTURE_CMD in full_skip_momo_commands,
            "CLI full skip-momo skipped Touch capture empty capture",
        )


def test_cli_half_flash_before_test() -> None:
    legacy_cfg = WorkstationConfig.from_dict({"flash_image_path": "legacy.hex"})
    _assert(legacy_cfg.half_flash_image_path == "legacy.hex", "legacy flash image did not migrate to half flash image")
    with tempfile.TemporaryDirectory() as tmp:
        half_image = str(Path(tmp) / "half-flow.hex")
        flash_calls: list[WorkstationConfig] = []
        exit_code, commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--flash-before-test",
                "--flash-image",
                half_image,
                "--flash-after-wait-s",
                "0",
            ],
            _half_success_responses(),
            fake_flash_ok=True,
            flash_calls_out=flash_calls,
        )
        _assert(exit_code == 0, f"CLI half flash success exit={exit_code}")
        _assert(flash_calls[0].flash_image_path == half_image, "half flow did not flash the half-specific image")
        _assert("AT+FACTORY=UNLOCK,TOKEN" in commands, "CLI half flash success did not continue into half flow")
        unified_paths = list(Path(tmp).glob("*/*/unified_log.csv"))
        _assert(unified_paths, "CLI half flash success did not write unified_log.csv")
        unified_text = unified_paths[0].read_text(encoding="utf-8")
        _assert("flash_start" in unified_text and "flash_end" in unified_text, "flash events missing from unified_log")
        _assert("Firmware flash" in unified_text, "flash step missing from unified_log")

    with tempfile.TemporaryDirectory() as tmp:
        exit_code, commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--flash-before-test",
                "--flash-after-wait-s",
                "0",
            ],
            _half_success_responses(),
            fake_flash_ok=False,
        )
        _assert(exit_code == 2, f"CLI half flash failure exit={exit_code}")
        _assert(commands == [], "CLI half flash failure sent AT commands after failed flash")
        unified_paths = list(Path(tmp).glob("*/*/unified_log.csv"))
        _assert(unified_paths, "CLI half flash failure did not write unified_log.csv")
        unified_text = unified_paths[0].read_text(encoding="utf-8")
        _assert("mock flash failed" in unified_text, "flash failure reason missing from unified_log")


def test_cli_half_flash_reconnect_failure_stops_flow() -> None:
    responses = {"AT": ["+CME ERROR:1,probe_failed"]}
    with tempfile.TemporaryDirectory() as tmp:
        exit_code, commands = _run_cli_with_responses(
            [
                "half",
                "--transport",
                "uart",
                "--port",
                "MOCK",
                "--records-root",
                tmp,
                "--sn",
                "SN001",
                "--token",
                "TOKEN",
                "--sn-record",
                "--flash-before-test",
                "--flash-after-wait-s",
                "0",
            ],
            responses,
            fake_flash_ok=True,
        )

    _assert(exit_code == 2, f"CLI half flash reconnect failure exit={exit_code}")
    _assert(commands == ["AT"], f"CLI flash reconnect failure sent unexpected commands: {commands}")


def test_flash_precheck_multi_probe_policy() -> None:
    original_run = flash_flow.subprocess.run

    class FakeCompleted:
        returncode = 0
        stdout = "11111111\n22222222\n"
        stderr = ""

    def fake_run(*_args, **_kwargs):
        return FakeCompleted()

    flash_flow.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "merged.hex"
            image.write_text(":00000001FF\n", encoding="ascii")
            config = WorkstationConfig(flash_image_path=str(image), jlink_probe_id="")
            formal = flash_flow.precheck_flash_request(config, sn_enabled=True, dry_run=False)
            dry = flash_flow.precheck_flash_request(config, sn_enabled=False, dry_run=True)

            _assert(not formal.ok and formal.level == "ERR", "formal multi-probe precheck did not block")
            _assert(dry.ok and dry.level == "WARN", "dry-run multi-probe precheck did not warn")

            script = Path(tmp) / "flash.ps1"
            script.write_text("exit 0\n", encoding="utf-8")
            script_config = WorkstationConfig(flash_backend="script", flash_script_path=str(script), jlink_probe_id="")
            script_result = flash_flow.precheck_flash_request(script_config, sn_enabled=True, dry_run=False)
            _assert(script_result.ok and script_result.level == "WARN", "script backend probe policy is too strict")

            empty_image = WorkstationConfig(flash_image_path="", jlink_probe_id="123")
            empty_image_result = flash_flow.precheck_flash_request(empty_image, sn_enabled=True, dry_run=False)
            _assert(not empty_image_result.ok and "empty" in empty_image_result.message, "empty flash image was accepted")

            directory_image = WorkstationConfig(flash_image_path=tmp, jlink_probe_id="123")
            directory_image_result = flash_flow.precheck_flash_request(directory_image, sn_enabled=True, dry_run=False)
            _assert(not directory_image_result.ok and "not a file" in directory_image_result.message, "directory flash image was accepted")

            empty_script = WorkstationConfig(flash_backend="script", flash_script_path="", jlink_probe_id="123")
            empty_script_result = flash_flow.precheck_flash_request(empty_script, sn_enabled=True, dry_run=False)
            _assert(not empty_script_result.ok and "empty" in empty_script_result.message, "empty flash script was accepted")

            directory_script = WorkstationConfig(flash_backend="script", flash_script_path=tmp, jlink_probe_id="123")
            directory_script_result = flash_flow.precheck_flash_request(directory_script, sn_enabled=True, dry_run=False)
            _assert(not directory_script_result.ok and "not a file" in directory_script_result.message, "directory flash script was accepted")
    finally:
        flash_flow.subprocess.run = original_run  # type: ignore[assignment]


def test_flash_runner_timeout_and_jlink_noise() -> None:
    original_popen = flash_runner.subprocess.Popen

    class TimeoutProc:
        def __init__(self, *_args, **_kwargs) -> None:
            self.returncode = None
            self.terminated = False
            self.killed = False

        def communicate(self, timeout=None):
            if timeout is not None and not self.terminated:
                raise subprocess.TimeoutExpired(cmd="mock", timeout=timeout, output="partial\n")
            self.returncode = -15 if not self.killed else -9
            return "rest\n", None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    class SuccessProc:
        def __init__(self, *_args, **_kwargs) -> None:
            self.returncode = 0

        def communicate(self, timeout=None):
            return "JLinkARM.dll reported error -256\nVerified OK\n", None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "merged.hex"
            image.write_text(":00000001FF\n", encoding="ascii")
            config = WorkstationConfig(flash_image_path=str(image), nrfjprog_path="mock-nrfjprog")
            config.flash_timeout_s = 1.0

            lines: list[tuple[str, str]] = []
            flash_runner.subprocess.Popen = TimeoutProc  # type: ignore[assignment]
            timeout_outcome = flash_runner.run_flash(config, lambda direction, line: lines.append((direction, line)))
            _assert(timeout_outcome.result == "NG", "timeout flash did not return NG")
            _assert("timeout" in timeout_outcome.message, "timeout flash message missing timeout")

            lines.clear()
            flash_runner.subprocess.Popen = SuccessProc  # type: ignore[assignment]
            ok_outcome = flash_runner.run_flash(config, lambda direction, line: lines.append((direction, line)))
            _assert(ok_outcome.ok, "JLinkARM -256 noise made flash fail")
            _assert(("FLASH_WARN", "JLinkARM.dll reported error -256") in lines, "JLinkARM -256 was not downgraded")
    finally:
        flash_runner.subprocess.Popen = original_popen  # type: ignore[assignment]


def test_cli_frame_output_filtering() -> None:
    args = [
        "full",
        "--transport",
        "uart",
        "--port",
        "MOCK",
        "--no-sn-record",
        "--no-ota",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code, _commands = _run_cli_with_responses([*args, "--records-root", tmp], _full_success_responses())
        output = buf.getvalue()
        _assert(exit_code == 0, f"CLI frame filtering exit={exit_code}")
        _assert("+HW:TOUCH:FRAME:" not in output, "CLI default printed Touch frame")
        _assert("+HW:IMU:VIBF:" not in output, "CLI default printed LRA frame")
        _assert("+HW:PPG:F:" not in output, "CLI default printed PPG frame")
        _assert("+HW:IMU:VIBSUMMARY:" in output, "CLI default hid summary line")

    with tempfile.TemporaryDirectory() as tmp:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code, _commands = _run_cli_with_responses([*args, "--records-root", tmp, "--verbose-frames"], _full_success_responses())
        output = buf.getvalue()
        _assert(exit_code == 0, f"CLI verbose frame exit={exit_code}")
        _assert("+HW:IMU:VIBF:" in output, "CLI verbose did not print LRA frame")


def test_same_hash_ota_is_pending() -> None:
    original_transport = transport_ble.BLENusTransport
    original_run_ota = ota_runner.run_ota
    transport_ble.BLENusTransport = FakeBleTransport  # type: ignore[assignment]
    ScriptedTransport.created.clear()

    def same_hash_ota(_config: WorkstationConfig, _ble_address: str, line_callback) -> int:
        line_callback("OTA", "Image upload skipped: new image matches the active image")
        return 1

    ota_runner.run_ota = same_hash_ota  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "dfu_application.zip"
            image.write_bytes(b"smoke")
            config = WorkstationConfig(
                ota_enabled=True,
                ota_image_path=str(image),
                ota_reboot_wait_s=0.0,
                factory_at_required=True,
            )
            record = FakeRecord()
            outcome = flows.run_full_machine(
                ATClient(FakeBleTransport()),
                config,
                "SN001",
                "TOKEN",
                record,  # type: ignore[arg-type]
                _progress,
            )
    finally:
        ota_runner.run_ota = original_run_ota  # type: ignore[assignment]
        transport_ble.BLENusTransport = original_transport  # type: ignore[assignment]

    all_commands = [cmd for transport in ScriptedTransport.created for cmd in transport.commands]
    _assert(outcome.result == "PENDING-HW", f"expected PENDING-HW, got {outcome.result}")
    _assert(not any(cmd.startswith("AT+FACTORY=UNLOCK") for cmd in all_commands), "same-hash OTA continued into factory unlock")
    _assert(record.finished is not None and record.finished[0] == "PENDING-HW", "record finish did not preserve PENDING-HW")


def test_real_ota_requires_busy_clear_after_reconnect() -> None:
    original_transport = transport_ble.BLENusTransport
    original_run_ota = ota_runner.run_ota
    transport_ble.BLENusTransport = FakeBleTransport  # type: ignore[assignment]
    ScriptedTransport.created.clear()

    def real_ota(_config: WorkstationConfig, _ble_address: str, line_callback) -> int:
        line_callback("OTA", "Image upload complete")
        return 0

    ota_runner.run_ota = real_ota  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "dfu_application.zip"
            image.write_bytes(b"smoke")
            config = WorkstationConfig(
                ota_enabled=True,
                ota_image_path=str(image),
                ota_reboot_wait_s=0.0,
                factory_at_required=True,
            )
            record = FakeRecord()
            outcome = flows._run_ota_phase(  # type: ignore[attr-defined]
                ATClient(FakeBleTransport()),
                config,
                record,  # type: ignore[arg-type]
                _progress,
                flows.StepContext(1),
            )
    finally:
        ota_runner.run_ota = original_run_ota  # type: ignore[assignment]
        transport_ble.BLENusTransport = original_transport  # type: ignore[assignment]

    all_commands = [cmd for transport in ScriptedTransport.created for cmd in transport.commands]
    _assert(outcome.result == "PASS", f"expected PASS, got {outcome.result}")
    _assert(all_commands.count("AT+OTABUSY?") >= 2, "OTA did not check AT+OTABUSY? after reconnect")


def test_ota_command_uses_dongle_backend() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        image = repo / "build_ondemand" / "dfu_application.zip"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"smoke")
        config = WorkstationConfig(
            firmware_repo=str(repo),
            ota_image_path=str(image),
            ble_name="AXI-P1-T",
            ble_scan_backend="nrf_dongle",
            ble_dongle_port="COM8",
            ble_dongle_sd_version="auto",
            nrf_connect_ble_path="C:/nrf-connect",
            ota_reboot_wait_s=15.0,
        )
        command = ota_runner.build_ota_command(config, "C8:B9:CA:AC:85:74")
        argv_text = " ".join(command.argv)
        _assert(command.script_name == "ota_smp_dongle.py", f"unexpected script {command.script_name}")
        _assert("--dongle-port COM8" in argv_text, "dongle port missing from OTA command")
        _assert("--verify-after-reset" in command.argv, "dongle OTA command does not verify reset")
        _assert("C8:B9:CA:AC:85:74" in command.argv, "BLE address missing from OTA command")

        config.ble_scan_backend = "windows"
        command = ota_runner.build_ota_command(config, "C8:B9:CA:AC:85:74")
        _assert(command.script_name == "ota_smp_ble.py", f"windows backend script {command.script_name}")


def test_ble_close_is_idempotent_without_loop() -> None:
    transport = object.__new__(transport_ble.BLENusTransport)
    transport._backend = "bleak"
    transport._closed = threading.Event()
    transport._loop = None
    transport._client = None
    transport._close_lock = threading.Lock()
    transport._disconnecting = False

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        transport.close()
        transport.close()
        loop = asyncio.new_event_loop()
        loop.close()
        transport._loop = loop
        transport.close()

    _assert(not caught, f"unexpected warnings: {caught}")


def test_dongle_backend_requires_address() -> None:
    raised = False
    try:
        transport_ble.BLENusTransport("AXI-P1-T", "", backend="nrf_dongle")
    except RuntimeError as exc:
        raised = "requires a BLE address" in str(exc)
    except Exception:
        raised = False
    _assert(raised, "nRF dongle backend did not require a BLE address")


def test_dongle_kwargs_property_and_close() -> None:
    transport = object.__new__(transport_ble.BLENusTransport)
    transport._backend = "nrf_dongle"
    transport._dongle_port = "COM9"
    transport._nrf_connect_ble_path = "/tmp/ble"
    transport._dongle_sd_version = "v5"
    transport._closed = threading.Event()
    transport._dongle_proc = None
    transport._dongle_lock = threading.Lock()

    kwargs = transport.dongle_kwargs
    _assert(kwargs == {
        "backend": "nrf_dongle",
        "dongle_port": "COM9",
        "nrf_connect_ble_path": "/tmp/ble",
        "dongle_sd_version": "v5",
    }, f"dongle_kwargs returned wrong dict: {kwargs}")
    _assert(transport.backend == "nrf_dongle", "backend property mismatch")

    transport.close()
    _assert(transport._dongle_proc is None, "dongle close left a process handle")


def main() -> int:
    tests = [
        test_sensitive_factory_tokens_are_redacted,
        test_engineer_password_and_saved_token,
        test_unlock_failure_cleanup,
        test_sn_disabled_half_flow_skips_sn_commands,
        test_sn_disabled_half_without_token_skips_factory_gate,
        test_sn_enabled_missing_token_still_fails,
        test_sn_persistence_failure_marks_half_ng,
        test_capability_step_numbering_and_single_cap_query,
        test_sn_disabled_full_without_token_skips_factory_gate,
        test_full_capture_timeouts_have_minimums,
        test_capture_output_mode_legacy_fallback,
        test_compact_parser_and_unified_log,
        test_split_record_output_writes_compatibility_files,
        test_cli_half_full_sn_modes,
        test_cli_half_flash_before_test,
        test_cli_half_flash_reconnect_failure_stops_flow,
        test_flash_precheck_multi_probe_policy,
        test_flash_runner_timeout_and_jlink_noise,
        test_cli_frame_output_filtering,
        test_ota_command_uses_dongle_backend,
        test_same_hash_ota_is_pending,
        test_real_ota_requires_busy_clear_after_reconnect,
        test_ble_close_is_idempotent_without_loop,
        test_dongle_backend_requires_address,
        test_dongle_kwargs_property_and_close,
    ]
    for test in tests:
        test()
    print("p1_0h_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
