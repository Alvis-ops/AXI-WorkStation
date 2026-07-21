from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factory_workstation import cli
    from factory_workstation import flash_flow
    from factory_workstation import flash_runner
    from factory_workstation import flows
    from factory_workstation.at_client import ATClient, CommandResult
    from factory_workstation.at_parser import is_capture_frame_line, parse_line
    from factory_workstation.config import (
        WorkstationConfig,
        get_factory_token,
        has_engineer_password,
        redact_sensitive_text,
        save_engineer_password,
        save_factory_token,
        verify_engineer_password,
    )
    from factory_workstation.flash_runner import FlashCommand, FlashOutcome
    from factory_workstation import ota_runner
    from factory_workstation import transport_ble
    from factory_workstation.storage import RunStorage, verify_half_sn_pass_record
else:
    from . import cli
    from . import flash_flow
    from . import flash_runner
    from . import flows
    from .at_client import ATClient, CommandResult
    from .at_parser import is_capture_frame_line, parse_line
    from .config import (
        WorkstationConfig,
        get_factory_token,
        has_engineer_password,
        redact_sensitive_text,
        save_engineer_password,
        save_factory_token,
        verify_engineer_password,
    )
    from .flash_runner import FlashCommand, FlashOutcome
    from . import ota_runner
    from . import transport_ble
    from .storage import RunStorage, verify_half_sn_pass_record


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
        "AT+VER?": ["+VER:version=0.0.1,build=smoke", "OK"],
        "AT+SN?": ["+SN:value=SN001,valid=1,source=lfs,production=0,ret=0", "OK"],
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT+OTABUSY?": ["+OTABUSY:locked=0", "OK"],
        "AT+OTA?": ["+OTA:locked=0,state=idle", "OK"],
        "AT+RST": ["+RST:delay_ms=200", "OK"],
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
            "AT+VER?": ["+VER:version=0.0.1,build=smoke", "OK"],
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


def _write_half_record(records_root: str | Path, sn: str = "SN001", result: str = "PASS", split: bool = False) -> Path:
    record = RunStorage(records_root, write_extra_files=split).start_run("HALF", sn, "")
    run_dir = record.run_dir
    record.finish(result, "completed" if result == "PASS" else "failed")
    return run_dir


def test_half_sn_record_check_requires_half_pass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = verify_half_sn_pass_record(tmp, "SN001")
        _assert(not missing.ok and "未找到" in missing.message, "missing half record was accepted")

        _write_half_record(tmp, "SN001", "NG")
        failed = verify_half_sn_pass_record(tmp, "SN001")
        _assert(not failed.ok and failed.result == "NG", "latest failed half record was accepted")

        pass_dir = _write_half_record(tmp, "SN001", "PASS")
        passed = verify_half_sn_pass_record(tmp, "SN001")
        _assert(passed.ok, f"half PASS record was rejected: {passed.message}")
        _assert(str(pass_dir / "unified_log.csv") == passed.record_path, "unified half record path mismatch")

    with tempfile.TemporaryDirectory() as tmp:
        split_dir = _write_half_record(tmp, "SN002", "PASS", split=True)
        passed = verify_half_sn_pass_record(tmp, "SN002")
        _assert(passed.ok, f"split half PASS record was rejected: {passed.message}")
        _assert(Path(passed.record_path).parent == split_dir, "split half record path mismatch")


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


def test_save_engineer_password_sha256_only() -> None:
    old_plain = os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD")
    old_hash = os.environ.get("AXI_FACTORY_ENGINEER_PASSWORD_SHA256")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from factory_workstation import config as config_module

            original_env_path = config_module.ENV_PATH
            config_module.ENV_PATH = Path(tmp) / ".env"
            try:
                os.environ.pop("AXI_FACTORY_ENGINEER_PASSWORD", None)
                os.environ.pop("AXI_FACTORY_ENGINEER_PASSWORD_SHA256", None)
                _assert(not has_engineer_password(WorkstationConfig()), "empty station should report no engineer password")
                save_engineer_password("first-setup-pass")
                env_text = config_module.ENV_PATH.read_text(encoding="utf-8")
                _assert("AXI_FACTORY_ENGINEER_PASSWORD_SHA256=" in env_text, "sha256 password was not saved")
                _assert("first-setup-pass" not in env_text, "plaintext password leaked into .env")
                plain_line = [
                    line
                    for line in env_text.splitlines()
                    if line.startswith("AXI_FACTORY_ENGINEER_PASSWORD=")
                    and not line.startswith("AXI_FACTORY_ENGINEER_PASSWORD_SHA256=")
                ]
                _assert(not plain_line or plain_line[0].endswith('=""') or plain_line[0].endswith("="), "plaintext password key should be empty")
                _assert(has_engineer_password(WorkstationConfig()), "saved password should be detected")
                _assert(verify_engineer_password("first-setup-pass", WorkstationConfig()), "saved sha256 password failed verify")
                _assert(not verify_engineer_password("wrong", WorkstationConfig()), "wrong password accepted after sha256 save")
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


def _half_success_responses() -> dict[str, list[str]]:
    return {
        "AT+CAP?": ["+CAP:factory_prod=1", "OK"],
        "AT": ["OK"],
        "AT+VER?": ["+VER:version=0.0.1,build=smoke", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        "AT+SN=SN001": ["OK"],
        "AT+SN?": ["+SN:value=SN001,valid=1,source=lfs,production=0,ret=0", "OK"],
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
        "AT+VER?": ["+VER:version=0.0.1,build=smoke", "OK"],
        "AT+SN?": ["+SN:value=SN001,valid=1,source=lfs,production=0,ret=0", "OK"],
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
        "AT+VER?": ["+VER:version=0.0.1,build=smoke", "OK"],
        "AT+FACTORY=UNLOCK,TOKEN": ["OK"],
        "AT+SN=SN001": ["OK"],
        "AT+SN?": ["+SN:value=SN001,valid=1,source=lfs,production=0,ret=0", "OK"],
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
    _assert(record.finished is None, "flow layer unexpectedly closed the missing-token record")


def test_sn_persistence_failure_marks_half_ng() -> None:
    responses = _half_success_responses()
    responses["AT+SN?"] = ["+SN:value=SN001,valid=1,source=ram,production=0,ret=0", "OK"]
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
    _assert(record.finished is None, "flow layer unexpectedly closed the SN-persistence record")
    sn_items = [item for item in record.items if item[1] == "SN persistence check"]
    _assert(sn_items and sn_items[-1][3] == "NG", "SN persistence item was not NG")


def test_full_machine_sn_match_passes() -> None:
    transport = ScriptedTransport(responses=_full_success_responses())
    record = FakeRecord()
    outcome = flows.run_full_machine(
        ATClient(transport),
        WorkstationConfig(factory_at_required=True, ota_enabled=False),
        "SN001",
        "TOKEN",
        record,  # type: ignore[arg-type]
        _progress,
        sn_enabled=True,
    )

    _assert(outcome.result == "PASS", f"matching full-flow SN expected PASS, got {outcome.result}")
    sn_items = [item for item in record.items if item[1] == "Read SN"]
    _assert(sn_items and sn_items[-1][3] == "PASS", "matching full-flow SN check was not PASS")


def test_full_machine_sn_mismatch_stops_flow() -> None:
    responses = _full_success_responses()
    responses["AT+SN?"] = ["+SN:value=SN002,valid=1,source=lfs,production=0,ret=0", "OK"]
    transport = ScriptedTransport(responses=responses)
    record = FakeRecord()
    outcome = flows.run_full_machine(
        ATClient(transport),
        WorkstationConfig(factory_at_required=True, ota_enabled=False),
        "SN001",
        "TOKEN",
        record,  # type: ignore[arg-type]
        _progress,
        sn_enabled=True,
    )

    _assert(outcome.result == "NG", f"mismatching full-flow SN expected NG, got {outcome.result}")
    _assert("SN mismatch" in outcome.message, f"unexpected SN mismatch message: {outcome.message}")
    _assert(TOUCH_CAPTURE_CMD not in transport.commands, "full flow continued after SN mismatch")
    sn_items = [item for item in record.items if item[1] == "Read SN"]
    _assert(sn_items and sn_items[-1][3] == "NG", "mismatching full-flow SN check was not NG")


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
        summary_line = "+HW:IMU:VIBSUMMARY:samples=150,duration_ms=3000,interval_ms=20,elapsed_ms=3020,overruns=0,max_late_ms=0,status=PASS"
        record.log_at("RX", summary_line)
        record.log_item("full", "Touch capture", TOUCH_CAPTURE_CMD, "PASS", 1234, "", "OK")
        record.finish("PASS", "completed")

        unified_path = run_dir / "unified_log.csv"
        rows = list(csv.DictReader(unified_path.open("r", encoding="utf-8")))
        event_types = {row["event_type"] for row in rows}
        _assert(
            {"flow_start", "step_start", "at_tx", "touch_frame", "vib_frame", "ppg_frame", "vib_summary", "step_end", "flow_end"}
            <= event_types,
            "unified_log missing events",
        )
        # B1a line-level: frame raw_line must not also appear as at_rx.
        frame_lines = {touch.line, vib.line, ppg.line}
        for row in rows:
            if row["event_type"] == "at_rx" and row["raw_line"] in frame_lines:
                raise AssertionError(f"frame line still recorded as at_rx: {row['raw_line']}")
        _assert({"touch_frame", "vib_frame", "ppg_frame"} <= event_types, "semantic frame events missing")
        _assert(any(row["event_type"] == "at_rx" and row["raw_line"] == summary_line for row in rows), "non-frame RX missing at_rx")
        unified_text = unified_path.read_text(encoding="utf-8")
        _assert("SECRET_TOKEN" not in unified_text, "unified_log leaked token")
        _assert("AT+FACTORY=UNLOCK,***" in unified_text, "unified_log did not retain redacted token")
        _assert({path.name for path in run_dir.iterdir() if path.is_file()} == {"unified_log.csv"}, "capture run wrote extra files")
        # B1: batch flush should not flush on every writerow for small runs, but close must persist.
        unified_sink = record.sinks.get("unified_log")
        _assert(unified_sink is not None, "unified_log sink missing")
        _assert(unified_sink.flush_count >= 1, "unified_log was never flushed")


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

        # B1b content-level: row counts and last-batch persistence after close.
        def _csv_rows(name: str) -> list[dict[str, str]]:
            return list(csv.DictReader((run_dir / name).open("r", encoding="utf-8")))

        momo_raw = _csv_rows("momo_raw.csv")
        momo_filt = _csv_rows("momo_filt.csv")
        lra_frames = _csv_rows("lra_frames.csv")
        ppg_frames = _csv_rows("ppg_frames.csv")
        capture_summary = _csv_rows("capture_summary.csv")
        _assert(len(momo_raw) == 1 and momo_raw[0]["raw0"] == "100", "momo_raw content incomplete")
        _assert(len(momo_filt) == 1 and momo_filt[0]["baseline3"] == "83", "momo_filt content incomplete")
        _assert(len(lra_frames) == 1 and lra_frames[0]["gx"] == "1", "lra_frames content incomplete")
        _assert(len(ppg_frames) == 1 and ppg_frames[0]["green0"] == "123", "ppg_frames content incomplete")
        _assert(len(capture_summary) >= 1 and capture_summary[-1]["kind"] == "vib_summary", "capture_summary missing vib_summary")
        raw_at = (run_dir / "raw_at.log").read_text(encoding="utf-8")
        _assert(touch_line in raw_at and summary_line in raw_at, "raw_at.log missing last-batch lines after close")
        for sink_name in ("unified_log", "lra_frames", "ppg_frames", "momo_raw"):
            sink = record.sinks.get(sink_name)
            _assert(sink is not None and sink.flush_count >= 1, f"{sink_name} not flushed on close")


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

    cli_args = list(args)
    # Local config.json may enable half_flash_before_test. Smoke cases that are
    # not explicitly testing flash must not inherit that and call real nrfjprog.
    flash_flags = {"--flash-before-test", "--no-flash-before-test"}
    if fake_flash_ok is None and not flash_flags.intersection(cli_args):
        cli_args.append("--no-flash-before-test")

    original_start_mes_run = cli.start_mes_run
    original_complete_mes_run = cli.complete_mes_run
    cli.start_mes_run = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[assignment]
        confirmed=True,
        process_started_at=datetime.now(),
        message="smoke MES route confirmed",
        route_checked=False,
    )
    cli.complete_mes_run = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[assignment]
        status=cli.MES_CONFIRMED,
        message="smoke MES result confirmed",
        pending_path="",
    )
    try:
        exit_code = cli.run(
            cli_args,
            transport_factory=factory,
            flash_runner=fake_flash if fake_flash_ok is not None else None,
        )
    finally:
        cli.start_mes_run = original_start_mes_run  # type: ignore[assignment]
        cli.complete_mes_run = original_complete_mes_run  # type: ignore[assignment]
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
                "--record-output-mode",
                "unified",
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
        _write_half_record(tmp, "SN001", "PASS")
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
                "--record-output-mode",
                "unified",
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
        exit_code, missing_half_commands = _run_cli_with_responses(
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
                "--record-output-mode",
                "unified",
            ],
            _full_success_responses(),
        )
        _assert(exit_code == 2, f"CLI full without half record exit={exit_code}")
        _assert(missing_half_commands == [], "CLI full without half record contacted device")
        _assert(not list(Path(tmp).glob("*")), "CLI full without half record wrote records")

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
        _write_half_record(tmp, "SN001", "PASS")
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
    original_tool = flash_runner._run_nrfjprog_tool

    class FakeCompleted:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    calls: list[list[str]] = []

    def fake_tool(_tool: str, args: list[str], timeout_s: float = 15.0):
        _ = timeout_s
        calls.append(args)
        return FakeCompleted("nrfjprog version: mock\n" if "--version" in args else "11111111\n22222222\n")

    flash_runner._run_nrfjprog_tool = fake_tool  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "merged.hex"
            image.write_text(":00000001FF\n", encoding="ascii")
            dll = Path(tmp) / "JLinkARM.dll"
            dll.write_bytes(b"mock")
            config = WorkstationConfig(flash_image_path=str(image), jlink_dll_path=str(dll), jlink_probe_id="")
            formal = flash_flow.precheck_flash_request(config, sn_enabled=True, dry_run=False)
            dry = flash_flow.precheck_flash_request(config, sn_enabled=False, dry_run=True)

            _assert(not formal.ok and formal.level == "ERR", "formal multi-probe precheck did not block")
            _assert(not dry.ok and dry.level == "ERR", "dry-run multi-probe precheck did not block")
            _assert(sum("--ids" in args for args in calls) == 2, "nrfjprog --ids was not run for each precheck")

            configured = WorkstationConfig(flash_image_path=str(image), jlink_dll_path=str(dll), jlink_probe_id="22222222")
            configured_result = flash_flow.precheck_flash_request(configured, sn_enabled=True, dry_run=False)
            _assert(configured_result.ok, "configured probe was not validated against --ids")

            script = Path(tmp) / "flash.ps1"
            script.write_text("exit 0\n", encoding="utf-8")
            script_config = WorkstationConfig(
                flash_backend="script",
                flash_script_path=str(script),
                jlink_dll_path=str(dll),
                jlink_probe_id="",
            )
            script_result = flash_flow.precheck_flash_request(script_config, sn_enabled=True, dry_run=False)
            _assert(not script_result.ok and script_result.level == "ERR", "script backend allowed ambiguous probes")

            script_config.jlink_probe_id = "22222222"
            script_selected = flash_flow.precheck_flash_request(script_config, sn_enabled=True, dry_run=False)
            _assert(script_selected.ok, "script backend did not validate the selected probe")

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
        flash_runner._run_nrfjprog_tool = original_tool  # type: ignore[assignment]


def test_jlink_scan_autofill_and_script_env() -> None:
    from factory_workstation import ui_main as ui_mod

    original_tool = flash_runner._run_nrfjprog_tool
    original_save_config = ui_mod.save_config

    class FakeCompleted:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def fake_tool(_tool: str, args: list[str], timeout_s: float = 15.0):
        _ = timeout_s
        return FakeCompleted("nrfjprog version: mock\n" if "--version" in args else "69730336\n")

    class FakeVar:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    flash_runner._run_nrfjprog_tool = fake_tool  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "flash.ps1"
            script.write_text("exit 0\n", encoding="utf-8")
            dll = Path(tmp) / "JLinkARM.dll"
            dll.write_bytes(b"mock")
            config = WorkstationConfig(
                flash_backend="script",
                flash_script_path=str(script),
                nrfjprog_path="mock-nrfjprog",
                jlink_dll_path=str(dll),
                jlink_probe_id="69730371",
            )

            scan = flash_runner.scan_jlink_probes(config)
            _assert(scan.ok and scan.probe_ids == ["69730336"], "manual scan did not ignore stale configured ID")
            strict = flash_runner.detect_jlink_probes(config)
            _assert(not strict.ok and "69730371" in strict.message, "formal precheck accepted a missing configured ID")

            config.jlink_probe_id = "69730336"
            selected = flash_flow.precheck_flash_request(config, sn_enabled=True, dry_run=False)
            _assert(selected.ok, "script backend rejected the scanned probe")
            command = flash_runner.build_flash_command(config)
            _assert(command.env.get("POC3A_JLINK_ID") == "69730336", "script command did not export selected probe ID")

            saved: list[str] = []
            logs: list[tuple[str, str]] = []
            popups: list[tuple[str, str, str]] = []
            ui_mod.save_config = lambda cfg: saved.append(cfg.jlink_probe_id)  # type: ignore[assignment]
            app = object.__new__(ui_mod.WorkstationApp)
            app.jlink_var = FakeVar("69730371")
            app.config_model = WorkstationConfig(jlink_probe_id="69730371")
            app._log = lambda level, message: logs.append((level, message))
            app._show_popup = lambda level, title, message: popups.append((level, title, message))
            app._apply_jlink_scan_result(scan)

            _assert(app.jlink_var.get() == "69730336", "GUI did not auto-fill scanned probe ID")
            _assert(app.config_model.jlink_probe_id == "69730336", "GUI model did not update scanned probe ID")
            _assert(saved == ["69730336"], "GUI did not persist scanned probe ID")
            _assert(logs and logs[-1][0] == "OK", "GUI did not log successful probe scan")
            _assert(popups and popups[-1][0] == "info", "GUI did not report successful probe scan")
    finally:
        flash_runner._run_nrfjprog_tool = original_tool  # type: ignore[assignment]
        ui_mod.save_config = original_save_config  # type: ignore[assignment]


def test_selected_image_flash_script_contract() -> None:
    selected_script = Path(__file__).with_name("flash_selected_image.ps1")
    _assert(selected_script.is_file(), "selected-image flash script is missing")
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "operator-selected.hex"
        image.write_text(":00000001FF\n", encoding="ascii")
        dll = Path(tmp) / "JLink_x64.dll"
        dll.write_bytes(b"mock")
        config = WorkstationConfig(
            flash_backend="script",
            flash_script_path=str(selected_script),
            flash_image_path=str(image),
            nrfjprog_path="mock-nrfjprog.exe",
            jlink_dll_path=str(dll),
            jlink_probe_id="69730336",
            flash_verify=False,
        )

        command = flash_runner.build_flash_command(config)
        _assert(command.image_path == str(image), "selected script did not retain the operator-selected image")
        _assert(command.image_sha256 == flash_runner.file_sha256(image), "selected image hash was not recorded")
        _assert(command.env.get("AXI_FLASH_IMAGE_PATH") == str(image), "selected image was not exported to script env")
        _assert(command.env.get("AXI_FLASH_NRFJPROG_PATH") == "mock-nrfjprog.exe", "nrfjprog path was not exported")
        _assert(command.env.get("AXI_FLASH_JLINK_DLL_PATH") == str(dll), "J-Link DLL was not exported")
        _assert(command.env.get("AXI_FLASH_VERIFY") == "0", "verify choice was not exported")
        _assert(command.env.get("POC3A_JLINK_ID") == "69730336", "probe ID was not exported")
        _assert(command.argv[command.argv.index("-ImagePath") + 1] == str(image), "selected image argument is wrong")
        _assert(command.argv[command.argv.index("-NrfjprogPath") + 1] == "mock-nrfjprog.exe", "tool argument is wrong")
        _assert(command.argv[command.argv.index("-JLinkDllPath") + 1] == str(dll), "DLL argument is wrong")
        _assert(command.argv[command.argv.index("-ProbeId") + 1] == "69730336", "probe argument is wrong")
        _assert("-NoVerify" in command.argv, "disabled verification was not passed to selected-image script")

        legacy_script = Path(tmp) / "legacy_flash.ps1"
        legacy_script.write_text("exit 0\n", encoding="utf-8")
        config.flash_script_path = str(legacy_script)
        legacy_command = flash_runner.build_flash_command(config)
        _assert("-ImagePath" not in legacy_command.argv, "legacy custom script received incompatible arguments")
        _assert(legacy_command.env.get("AXI_FLASH_IMAGE_PATH") == str(image), "legacy script cannot opt into selected-image env")

        config.flash_script_path = str(selected_script)
        config.flash_image_path = str(Path(tmp) / "missing.hex")
        try:
            flash_runner.build_flash_command(config)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("selected-image script accepted a missing image")


def test_at_client_suppresses_repeated_numeric_noise() -> None:
    class NoiseTransport:
        def __init__(self) -> None:
            self.lines = ["0002"] * 20 + ["OK"]

        def clear_input(self) -> None:
            pass

        def write_line(self, _command: str) -> None:
            pass

        def read_line(self, _timeout_s: float) -> str | None:
            return self.lines.pop(0) if self.lines else None

        def close(self) -> None:
            pass

    emitted: list[tuple[str, str]] = []
    result = ATClient(NoiseTransport(), lambda direction, line: emitted.append((direction, line))).send_command("AT", 1.0)
    _assert(result.ok, "AT did not recover after repeated numeric boot noise")
    _assert(result.ignored_lines == 20, "numeric boot noise was not counted as ignored")
    _assert(emitted.count(("RX", "0002")) == 1, "repeated numeric boot noise flooded the callback")
    _assert(("RX", "OK") in emitted, "valid AT response was suppressed with boot noise")


def test_ui_flash_reconnect_retries_with_fresh_client() -> None:
    from factory_workstation import ui_main as ui_mod

    original_probe = ui_mod.probe_at_client
    original_sleep = ui_mod.time.sleep

    class FakeVar:
        def get(self) -> str:
            return "UART"

    class FakeClient:
        def __init__(self, attempt: int) -> None:
            self.attempt = attempt
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def wait_closed(self, _timeout_s: float) -> bool:
            return True

    class FakeRecord:
        def __init__(self) -> None:
            self.starts: list[tuple] = []
            self.items: list[tuple] = []

        def start_step(self, *args) -> None:
            self.starts.append(args)

        def log_item(self, *args) -> None:
            self.items.append(args)

    clients = [FakeClient(1), FakeClient(2), FakeClient(3)]
    app = object.__new__(ui_mod.WorkstationApp)
    app.transport_var = FakeVar()
    app.events = queue.Queue()
    app.client = None
    app._make_client_from_current_settings = lambda _line_cb: (clients.pop(0), "UART", "COM21@460800")
    record = FakeRecord()
    progress_events: list[tuple] = []

    def fake_probe(client):
        if client.attempt < 3:
            return False, 50, "", f"attempt {client.attempt} not ready", []
        return True, 50, "+VER:test ; OK", "", []

    ui_mod.probe_at_client = fake_probe  # type: ignore[assignment]
    ui_mod.time.sleep = lambda _seconds: None  # type: ignore[assignment]
    try:
        selected = app._reconnect_after_flash(record, lambda *_args: None, lambda *args: progress_events.append(args))
    finally:
        ui_mod.probe_at_client = original_probe  # type: ignore[assignment]
        ui_mod.time.sleep = original_sleep  # type: ignore[assignment]

    _assert(selected is not None and selected.attempt == 3, "flash reconnect did not reach the successful retry")
    _assert(app.client is selected, "successful reconnect client was not installed")
    _assert(record.starts == [(2, "Flash reconnect", "AT;AT+VER?")], "reconnect step was not started explicitly")
    _assert(record.items and record.items[-1][3] == "PASS", "successful retry was not recorded as PASS")
    _assert(progress_events[-1][2] == "PASS", "reconnect progress did not end in PASS")


def test_flash_runner_timeout_and_jlink_noise() -> None:
    original_popen = flash_runner.subprocess.Popen
    original_tool = flash_runner._run_nrfjprog_tool

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

    class NoDebuggerProc:
        def __init__(self, *_args, **_kwargs) -> None:
            self.returncode = 41

        def communicate(self, timeout=None):
            return "No debuggers were discovered.\n", None

    class FakeCompleted:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def fake_tool(_tool: str, args: list[str], timeout_s: float = 15.0):
        _ = timeout_s
        return FakeCompleted("nrfjprog version: mock\n" if "--version" in args else "69730371\n")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "merged.hex"
            image.write_text(":00000001FF\n", encoding="ascii")
            dll = Path(tmp) / "JLinkARM.dll"
            dll.write_bytes(b"mock")
            config = WorkstationConfig(flash_image_path=str(image), nrfjprog_path="mock-nrfjprog", jlink_dll_path=str(dll))
            config.flash_timeout_s = 1.0
            flash_runner._run_nrfjprog_tool = fake_tool  # type: ignore[assignment]

            lines: list[tuple[str, str]] = []
            flash_runner.subprocess.Popen = TimeoutProc  # type: ignore[assignment]
            timeout_outcome = flash_runner.run_flash(config, lambda direction, line: lines.append((direction, line)))
            _assert(timeout_outcome.result == "NG", "timeout flash did not return NG")
            _assert("timeout" in timeout_outcome.message, "timeout flash message missing timeout")
            _assert(timeout_outcome.jlink_probe_id == "69730371", "single detected probe was not selected")
            _assert(timeout_outcome.command is not None, "flash command was not recorded")
            _assert("--verify" in timeout_outcome.command.argv and "--reset" in timeout_outcome.command.argv, "verify/reset missing")
            _assert("--snr" in timeout_outcome.command.argv, "selected SNR missing from flash command")
            _assert(any(direction == "PREFLIGHT" for direction, _line in lines), "preflight output was not logged")

            lines.clear()
            flash_runner.subprocess.Popen = SuccessProc  # type: ignore[assignment]
            ok_outcome = flash_runner.run_flash(config, lambda direction, line: lines.append((direction, line)))
            _assert(ok_outcome.ok, "JLinkARM -256 noise made flash fail")
            _assert(("FLASH_WARN", "JLinkARM.dll reported error -256") in lines, "JLinkARM -256 was not downgraded")

            flash_runner.subprocess.Popen = NoDebuggerProc  # type: ignore[assignment]
            no_debugger_outcome = flash_runner.run_flash(config)
            _assert(no_debugger_outcome.exit_code == 41, "nrfjprog exit 41 was lost")
            _assert("未检测到 J-Link" in no_debugger_outcome.message, "exit 41 was not translated")
    finally:
        flash_runner.subprocess.Popen = original_popen  # type: ignore[assignment]
        flash_runner._run_nrfjprog_tool = original_tool  # type: ignore[assignment]


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
    _assert(record.finished is None, "flow layer unexpectedly closed the same-hash OTA record")


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
        dongle_script = repo / "tools" / "ota_smp_dongle.py"
        dongle_script.parent.mkdir(parents=True, exist_ok=True)
        dongle_script.write_text("# smoke\n", encoding="utf-8")
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
        config.ble_pairing_enabled = True
        command = ota_runner.build_ota_command(config, "C8:B9:CA:AC:85:74")
        _assert(command.script_name == "ota_smp_ble.py", f"windows backend script {command.script_name}")
        _assert("--pair" in command.argv, "pairing flag missing from Windows OTA command")

        bundled_helper = repo / "Axi OTA Helper.exe"
        bundled_helper.write_bytes(b"MZ")
        command = ota_runner.build_ota_command(config, "C8:B9:CA:AC:85:74")
        _assert(command.script_name == "Axi OTA Helper.exe", f"bundled helper not selected {command.script_name}")
        _assert(command.argv[0] == str(bundled_helper), "bundled OTA helper executable is not argv[0]")


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


def test_dongle_backend_rejects_pairing() -> None:
    raised = False
    try:
        transport_ble.BLENusTransport(
            "AXI-P1-T",
            "C8:B9:CA:AC:85:74",
            backend="nrf_dongle",
            pair=True,
        )
    except RuntimeError as exc:
        raised = "Windows BLE backend" in str(exc)
    except Exception:
        raised = False
    _assert(raised, "nRF dongle backend silently accepted host pairing")


def test_windows_ble_pairing_requests_authenticated_link() -> None:
    import bleak

    seen: dict[str, object] = {}

    class FakeBleakClient:
        def __init__(self, target, **kwargs) -> None:
            seen["target"] = target
            seen["init"] = kwargs
            self.is_connected = True

        async def connect(self, **kwargs) -> None:
            seen["connect"] = kwargs

        async def start_notify(self, uuid, _callback) -> None:
            seen["notify"] = uuid

        async def disconnect(self) -> None:
            self.is_connected = False

    async def fake_scan(_name: str, _timeout: float) -> list[transport_ble.BLEDeviceInfo]:
        return [
            transport_ble.BLEDeviceInfo(
                "AXI-P1-T",
                "E3:A9:F3:49:97:A7",
                device="fake-device",
            )
        ]

    async def exercise() -> None:
        item = object.__new__(transport_ble.BLENusTransport)
        item.name = "AXI-P1-T"
        item.address = "E3:A9:F3:49:97:A7"
        item.scan_timeout_s = 8.0
        item._pair = True
        item._client = None
        item._rx = bytearray()
        await item._connect_main()

    original_client = bleak.BleakClient
    original_scan = transport_ble._scan_ble_async
    bleak.BleakClient = FakeBleakClient  # type: ignore[assignment]
    transport_ble._scan_ble_async = fake_scan  # type: ignore[assignment]
    try:
        asyncio.run(exercise())
    finally:
        bleak.BleakClient = original_client  # type: ignore[assignment]
        transport_ble._scan_ble_async = original_scan  # type: ignore[assignment]

    init_kwargs = seen.get("init", {})
    connect_kwargs = seen.get("connect", {})
    _assert(isinstance(init_kwargs, dict) and init_kwargs.get("pair") is True, "Bleak pair flag missing")
    _assert(
        isinstance(connect_kwargs, dict) and connect_kwargs.get("protection_level") == 3,
        "Windows BLE did not request encryption+authentication",
    )


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


def test_ui_log_batch_op_filter_and_poll_split() -> None:
    from factory_workstation import ui_main as ui_mod

    app = object.__new__(ui_mod.WorkstationApp)
    app.engineering_mode = False
    app._resize_active = False
    app._log_autoscroll_deferred = False
    app._log_line_count = 0
    app._ui_metrics = {"insert_calls": 0, "see_calls": 0, "ticks": 0, "control_events": 0, "log_events": 0}
    app.events = queue.Queue()
    app.client = None
    app.active_flow_kind = ""
    app.active_flow_sn = ""
    app.last_half_sn = ""

    class FakeText:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def insert(self, _index, text: str) -> None:
            self.chunks.append(text)

        def delete(self, *_args) -> None:
            return None

        def see(self, *_args) -> None:
            return None

    app.log_text = FakeText()
    _assert(not app._should_render_log("TX", "AT"), "OP mode should hide TX")
    _assert(not app._should_render_log("RX", "OK"), "OP mode should hide RX")
    _assert(app._should_render_log("INFO", "step"), "OP mode should show INFO")
    app.engineering_mode = True
    _assert(app._should_render_log("TX", "AT"), "engineering mode should show TX")
    app.engineering_mode = False

    app._append_log_lines(["[INFO] a", "[INFO] b"])
    _assert(app._ui_metrics["insert_calls"] == 1, "batch insert expected one call")
    _assert(app._ui_metrics["see_calls"] == 1, "batch see expected one call")

    order: list[str] = []
    app._set_busy = lambda value: order.append(f"busy:{value}")  # type: ignore[method-assign]
    app._put_step = lambda *args: order.append(f"step:{args[0]}")  # type: ignore[method-assign]
    app._show_flow_done_popup = lambda _outcome: order.append("flow_popup")  # type: ignore[method-assign]
    app.after = lambda _ms, _fn: None  # type: ignore[method-assign]

    app.events.put(("log", "INFO", "before"))
    app.events.put(("step", 1, "AT probe", "RUN", "cmd"))
    app.events.put(("log", "INFO", "after"))
    app.events.put(("busy", True))
    before_insert = app._ui_metrics["insert_calls"]
    app._poll_events()
    _assert(order[:2] == ["step:1", "busy:True"], f"control events not prioritized: {order}")
    _assert(app._ui_metrics["insert_calls"] == before_insert + 1, "poll should batch logs into one insert")
    _assert(app._ui_metrics["log_events"] == 2, "both log events should be counted")
    _assert(app._ui_metrics["control_events"] >= 2, "control events should be counted")


def test_ui_confirmed_mes_upload_popup_is_explicit() -> None:
    from factory_workstation import ui_main as ui_mod

    app = object.__new__(ui_mod.WorkstationApp)
    app.sn_var = SimpleNamespace(get=lambda: "SN-MES-001")
    app.sn_enabled_var = SimpleNamespace(get=lambda: True)

    calls: list[tuple[str, str, str]] = []
    original_showinfo = ui_mod.messagebox.showinfo
    ui_mod.messagebox.showinfo = lambda title, message: calls.append(("info", title, message))  # type: ignore[assignment]
    try:
        app._show_flow_done_popup(
            flows.FlowOutcome(
                True,
                "PASS",
                "all steps passed",
                [],
                mes_status=ui_mod.MES_CONFIRMED,
                mes_message="postxtdata confirmed: res='OK'",
            )
        )
    finally:
        ui_mod.messagebox.showinfo = original_showinfo  # type: ignore[assignment]

    _assert(len(calls) == 1, f"expected one success popup, got {calls}")
    _assert("MES 上传成功" in calls[0][1], f"popup title does not confirm MES upload: {calls[0]}")
    _assert("MES 数据上传成功" in calls[0][2], f"popup body does not confirm MES upload: {calls[0]}")


def test_ui_ota_releases_existing_connection_before_subprocess() -> None:
    from factory_workstation import ui_main as ui_mod

    order: list[str] = []

    class FakeClient:
        def __init__(self) -> None:
            self._transport = object.__new__(transport_ble.BLENusTransport)
            self._transport.address = "E3:A9:F3:49:97:A7"

        def send_command(self, command: str, _timeout_s: float):
            _assert(command == "AT+RST", f"unexpected OTA handoff command: {command}")
            order.append("reset")
            return SimpleNamespace(ok=True)

        def close(self) -> None:
            order.append("close")

        def wait_closed(self, _timeout_s: float) -> bool:
            order.append("wait_closed")
            return True

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "zephyr.signed.bin"
        image.write_bytes(b"smoke")
        app = object.__new__(ui_mod.WorkstationApp)
        app.busy = False
        app.client = FakeClient()
        app.config_model = WorkstationConfig(
            firmware_repo=tmp,
            ota_image_path=str(image),
            ble_scan_backend="windows",
        )
        app.ble_addr_var = SimpleNamespace(get=lambda: "E3:A9:F3:49:97:A7")
        app.events = queue.Queue()
        app._save_settings = lambda silent=False: None  # type: ignore[method-assign]
        app._set_busy = lambda _value: None  # type: ignore[method-assign]
        app._set_connection_status = lambda *_args: None  # type: ignore[method-assign]

        run_started = threading.Event()
        original_askyesno = ui_mod.messagebox.askyesno
        original_run_ota = ui_mod.run_ota
        original_wait_s = ui_mod.OTA_BLE_RELEASE_WAIT_S

        def fake_run_ota(_config, _address, _line_callback) -> int:
            order.append("run_ota")
            run_started.set()
            return 0

        ui_mod.messagebox.askyesno = lambda *_args, **_kwargs: True  # type: ignore[assignment]
        ui_mod.run_ota = fake_run_ota  # type: ignore[assignment]
        ui_mod.OTA_BLE_RELEASE_WAIT_S = 0.0
        try:
            app._run_ota()
            _assert(run_started.wait(2.0), "OTA worker did not start")
        finally:
            ui_mod.messagebox.askyesno = original_askyesno  # type: ignore[assignment]
            ui_mod.run_ota = original_run_ota  # type: ignore[assignment]
            ui_mod.OTA_BLE_RELEASE_WAIT_S = original_wait_s

    _assert(app.client is None, "GUI kept the existing BLE client during OTA")
    _assert(
        order == ["reset", "close", "wait_closed", "run_ota"],
        f"OTA connection release order is wrong: {order}",
    )


def test_cli_standalone_ota_handoff_and_preview() -> None:
    order: list[str] = []

    class FakeClient:
        def send_command(self, command: str, _timeout_s: float):
            _assert(command == "AT+RST", f"unexpected standalone OTA command: {command}")
            order.append("reset")
            return SimpleNamespace(ok=True, lines=["+RST:delay_ms=200", "OK"])

        def close(self) -> None:
            order.append("close")

        def wait_closed(self, _timeout_s: float) -> bool:
            order.append("wait_closed")
            return True

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "zephyr.signed.bin"
        image.write_bytes(b"smoke")
        config = WorkstationConfig(
            firmware_repo=tmp,
            ota_image_path=str(image),
            ble_name="AXI-P1-T",
        )
        args = SimpleNamespace(
            ble_address="E3:A9:F3:49:97:A7",
            ble_backend="bleak",
            ota_dry_run=False,
            ota_handoff=True,
            ota_skip_handoff=False,
            verbose_frames=False,
        )

        def factory(_config, _args, _line_callback):
            order.append("connect")
            return FakeClient()

        def fake_ota(_config, _address, _line_callback) -> int:
            order.append("run_ota")
            return 0

        original_wait_s = cli.OTA_CLI_HANDOFF_WAIT_S
        cli.OTA_CLI_HANDOFF_WAIT_S = 0.0
        try:
            code = cli._run_ota_command(  # type: ignore[attr-defined]
                config,
                args,
                transport_factory=factory,
                ota_runner=fake_ota,
            )
        finally:
            cli.OTA_CLI_HANDOFF_WAIT_S = original_wait_s

        _assert(code == 0, f"standalone OTA CLI returned {code}")
        _assert(
            order == ["connect", "reset", "close", "wait_closed", "run_ota"],
            f"standalone OTA CLI order is wrong: {order}",
        )

        args.ota_dry_run = True
        preview_factory_called = False

        def preview_factory(_config, _args, _line_callback):
            nonlocal preview_factory_called
            preview_factory_called = True
            raise AssertionError("OTA preview opened a transport")

        with contextlib.redirect_stdout(io.StringIO()):
            preview_code = cli._run_ota_command(  # type: ignore[attr-defined]
                config,
                args,
                transport_factory=preview_factory,
                ota_runner=fake_ota,
            )
        _assert(preview_code == 0, f"standalone OTA preview returned {preview_code}")
        _assert(not preview_factory_called, "OTA preview connected to hardware")

        args.ota_dry_run = False
        args.ota_handoff = False
        order.clear()
        direct_code = cli._run_ota_command(  # type: ignore[attr-defined]
            config,
            args,
            transport_factory=preview_factory,
            ota_runner=fake_ota,
        )
        _assert(direct_code == 0, f"direct standalone OTA returned {direct_code}")
        _assert(order == ["run_ota"], f"direct standalone OTA unexpectedly used NUS handoff: {order}")
        _assert(not preview_factory_called, "direct standalone OTA opened a NUS transport")


def main() -> int:
    tests = [
        test_half_sn_record_check_requires_half_pass,
        test_sensitive_factory_tokens_are_redacted,
        test_engineer_password_and_saved_token,
        test_save_engineer_password_sha256_only,
        test_unlock_failure_cleanup,
        test_sn_disabled_half_flow_skips_sn_commands,
        test_sn_disabled_half_without_token_skips_factory_gate,
        test_sn_enabled_missing_token_still_fails,
        test_sn_persistence_failure_marks_half_ng,
        test_full_machine_sn_match_passes,
        test_full_machine_sn_mismatch_stops_flow,
        test_capability_step_numbering_and_single_cap_query,
        test_sn_disabled_full_without_token_skips_factory_gate,
        test_full_capture_timeouts_have_minimums,
        test_capture_output_mode_legacy_fallback,
        test_compact_parser_and_unified_log,
        test_split_record_output_writes_compatibility_files,
        test_ui_log_batch_op_filter_and_poll_split,
        test_ui_confirmed_mes_upload_popup_is_explicit,
        test_ui_ota_releases_existing_connection_before_subprocess,
        test_cli_standalone_ota_handoff_and_preview,
        test_cli_half_full_sn_modes,
        test_cli_half_flash_before_test,
        test_cli_half_flash_reconnect_failure_stops_flow,
        test_flash_precheck_multi_probe_policy,
        test_jlink_scan_autofill_and_script_env,
        test_selected_image_flash_script_contract,
        test_at_client_suppresses_repeated_numeric_noise,
        test_ui_flash_reconnect_retries_with_fresh_client,
        test_flash_runner_timeout_and_jlink_noise,
        test_cli_frame_output_filtering,
        test_ota_command_uses_dongle_backend,
        test_same_hash_ota_is_pending,
        test_real_ota_requires_busy_clear_after_reconnect,
        test_ble_close_is_idempotent_without_loop,
        test_dongle_backend_requires_address,
        test_dongle_backend_rejects_pairing,
        test_windows_ble_pairing_requests_authenticated_link,
        test_dongle_kwargs_property_and_close,
    ]
    for test in tests:
        test()
    print("p1_0h_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
