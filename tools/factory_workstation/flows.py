from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Union

from .at_parser import is_capture_frame_line, parse_line
from .at_client import ATClient, CommandResult
from .config import WorkstationConfig, redact_sensitive_text
from .storage import NullRunRecord, RunRecord


ProgressCallback = Callable[[int, str, str, str], None]
StepPromptCallback = Callable[[str, str], None]
RecordSink = Union[RunRecord, NullRunRecord]


@dataclass
class FlowStep:
    label: str
    command: str
    timeout_s: float | None = None
    required: bool = True
    validator: Callable[[CommandResult], tuple[bool, str]] | None = None


@dataclass
class FlowOutcome:
    ok: bool
    result: str
    message: str
    results: list[CommandResult]
    mes_status: str = "SKIPPED"
    mes_message: str = ""
    mes_pending_path: str = ""


@dataclass
class StepContext:
    next_index: int = 1

    def take(self) -> int:
        index = self.next_index
        self.next_index += 1
        return index


def _filter_optional_steps(steps: list[FlowStep], skip_momo: bool) -> list[FlowStep]:
    if not skip_momo:
        return steps
    return [step for step in steps if step.label not in {"Touch ISR"}]


def _capture_command(config: WorkstationConfig, legacy: str, compact: str) -> str:
    mode = (config.capture_output_mode or "compact").strip().lower()
    return compact if mode == "compact" else legacy


def _display_lines(lines: list[str], limit: int = 3) -> list[str]:
    filtered = [line for line in lines if not is_capture_frame_line(line)]
    return (filtered or lines)[-limit:]


def _run_steps(
    client: ATClient,
    config: WorkstationConfig,
    steps: list[FlowStep],
    progress: ProgressCallback,
    record: RecordSink,
    station_type: str,
    before_step: StepPromptCallback | None = None,
    step_context: StepContext | None = None,
) -> FlowOutcome:
    context = step_context or StepContext()
    results: list[CommandResult] = []
    for step in steps:
        index = context.take()
        if before_step is not None:
            before_step(step.label, station_type)
        record.start_step(index, step.label, step.command)
        progress(index, step.label, "RUN", redact_sensitive_text(step.command))
        timeout = step.timeout_s if step.timeout_s is not None else config.at_timeouts.for_command(step.command)
        t0 = time.monotonic()
        result = client.send_command(step.command, timeout)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        results.append(result)
        validation_error = ""
        step_ok = result.ok
        if step_ok and step.validator is not None:
            step_ok, validation_error = step.validator(result)
        status = "PASS" if step_ok else ("WARN" if not step.required else "NG")
        detail = f"{elapsed_ms / 1000:.1f}s"
        if result.lines:
            detail += " | " + " ; ".join(_display_lines(result.lines))
        progress(index, step.label, status, detail)
        error_reason = "" if step_ok else (validation_error or (result.lines[-1] if result.lines else "timeout"))
        response_summary = " ; ".join(_display_lines(result.lines)) if result.lines else ""
        record.log_item(station_type, step.label, step.command, status, elapsed_ms, error_reason, response_summary)
        if step.required and not step_ok:
            return FlowOutcome(False, "NG", validation_error or f"{step.label} failed", results)
    return FlowOutcome(True, "PASS", "completed", results)


def _factory_unlocked(results: list[CommandResult]) -> bool:
    unlocked = False
    for result in results:
        command = result.command.upper().strip()
        if command.startswith("AT+FACTORY=UNLOCK") and result.ok:
            unlocked = True
        elif command.startswith("AT+FACTORY=LOCK") and result.ok:
            unlocked = False
    return unlocked


def _with_factory_lock_cleanup(
    client: ATClient,
    config: WorkstationConfig,
    record: RecordSink,
    station_type: str,
    progress: ProgressCallback,
    step_context: StepContext,
    outcome: FlowOutcome,
) -> FlowOutcome:
    if not _factory_unlocked(outcome.results):
        return outcome

    label = "Factory lock cleanup"
    command = "AT+FACTORY=LOCK"
    index = step_context.take()
    record.start_step(index, label, command)
    progress(index, label, "RUN", command)
    t0 = time.monotonic()
    result = client.send_command(command, config.at_timeouts.default_s)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    detail = f"{elapsed_ms / 1000:.1f}s"
    if result.lines:
        detail += " | " + " ; ".join(result.lines[-3:])
    status = "PASS" if result.ok else "NG"
    progress(index, label, status, detail)
    error_reason = "" if result.ok else (result.lines[-1] if result.lines else "timeout")
    record.log_item(
        station_type,
        label,
        command,
        status,
        elapsed_ms,
        error_reason,
        " ; ".join(result.lines[-3:]) if result.lines else "",
    )

    results = outcome.results + [result]
    if result.ok:
        return FlowOutcome(outcome.ok, outcome.result, outcome.message, results)
    message = f"{outcome.message}; factory lock cleanup failed"
    return FlowOutcome(False, "NG", message, results)


def _joined_lines(result: CommandResult) -> str:
    return " ; ".join(result.lines)


def _sn_match_validator(expected_sn: str) -> Callable[[CommandResult], tuple[bool, str]]:
    def validate(result: CommandResult) -> tuple[bool, str]:
        sn_value = ""
        sn_valid = ""
        for line in result.lines:
            parsed = parse_line(line)
            if parsed.kind != "sn":
                continue
            sn_value = parsed.fields.get("value", "")
            sn_valid = parsed.fields.get("valid", "")
            break

        if sn_valid != "1":
            return False, f"device SN invalid: valid={sn_valid or 'missing'}"
        if sn_value != expected_sn:
            return False, f"SN mismatch: expected={expected_sn}, actual={sn_value or '<empty>'}"
        return True, ""

    return validate


def _ota_state_locked(text: str) -> bool:
    upper = text.upper()
    return "LOCKED=1" in upper or "PENDING_RESET" in upper


def _ota_state_clear(text: str) -> bool:
    upper = text.upper()
    return "LOCKED=0" in upper and "PENDING_RESET" not in upper


def _reconnect_nus(
    client: ATClient,
    ble_name: str,
    ble_address: str,
    ble_scan_timeout: float,
    record: RecordSink,
    progress: ProgressCallback,
    index: int,
    probe: bool,
    ble_backend_kwargs: dict | None = None,
) -> tuple[bool, CommandResult | None, str]:
    from .transport_ble import BLENusTransport

    record.start_step(index, "OTA reconnect NUS", "AT")
    progress(index, "OTA reconnect NUS", "RUN", f"scanning {ble_name}")
    t0 = time.monotonic()
    try:
        new_transport = BLENusTransport(ble_name, ble_address, ble_scan_timeout, **(ble_backend_kwargs or {}))
        client.replace_transport(new_transport)
        probe_result = client.send_command("AT", 8.0) if probe else None
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record.log_item("full", "OTA reconnect NUS", "BLENusTransport", "NG", elapsed_ms, str(exc), "")
        progress(index, "OTA reconnect NUS", "NG", str(exc))
        return False, None, str(exc)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    if probe_result is not None and not probe_result.ok:
        detail = _joined_lines(probe_result)
        record.log_item("full", "OTA reconnect NUS", "AT", "NG", elapsed_ms, "probe failed", detail)
        progress(index, "OTA reconnect NUS", "NG", detail or f"elapsed={elapsed_ms}ms")
        return False, probe_result, "probe failed"

    detail = _joined_lines(probe_result) if probe_result is not None else ""
    record.log_item("full", "OTA reconnect NUS", "AT", "PASS", elapsed_ms, "", detail)
    progress(index, "OTA reconnect NUS", "PASS", detail or f"elapsed={elapsed_ms}ms")
    return True, probe_result, ""


def _check_factory_capability(
    client: ATClient,
    record: RecordSink,
    station_type: str,
    progress: ProgressCallback,
    step_context: StepContext,
) -> tuple[bool, CommandResult]:
    index = step_context.take()
    record.start_step(index, "Factory AT capability", "AT+CAP?")
    progress(index, "Factory AT capability", "RUN", "AT+CAP?")
    cap = client.send_command("AT+CAP?", 8.0)
    cap_line = " ; ".join(cap.lines)
    factory_ok = cap.ok and "factory_prod=1" in cap_line
    record.log_item(station_type, "Factory AT capability", "AT+CAP?",
                    "PASS" if factory_ok else "NG", int(cap.elapsed_s * 1000),
                    "" if factory_ok else "factory_prod not available", cap_line)
    progress(index, "Factory AT capability", "PASS" if factory_ok else "NG",
             " ; ".join(cap.lines[-2:]))
    return factory_ok, cap


def _missing_required_token(config: WorkstationConfig, sn_enabled: bool, token: str) -> bool:
    return config.factory_at_required and sn_enabled and not token


def _half_chip_communication_steps(config: WorkstationConfig) -> list[FlowStep]:
    short_timeout = config.at_timeouts.hw_short_s
    return [
        FlowStep("IMU communication", "AT+HW=IMU,PROBE", short_timeout),
        FlowStep("Touch communication", "AT+HW=TOUCH,PROBE", short_timeout),
        FlowStep("Charger communication", "AT+HW=CHG,REGS", short_timeout),
        FlowStep("Gauge communication", "AT+HW=GAUGE,DATA", short_timeout),
        FlowStep("Flash communication", "AT+HW=FLASH,PROBE", short_timeout),
        FlowStep("PPG communication", "AT+HW=PPG,PROBE", short_timeout),
    ]


def _full_capture_steps(config: WorkstationConfig) -> list[FlowStep]:
    return [
        FlowStep(
            "Touch capture",
            _capture_command(
                config,
                "AT+HW=TOUCH,CAPTURE,CONFIRM,3000",
                "AT+HW=TOUCH,CAPTURE,CONFIRM,3000,COMPACT",
            ),
            config.at_timeouts.touch_capture_timeout_s(),
        ),
        FlowStep(
            "LRA vibcapture",
            _capture_command(
                config,
                "AT+HW=IMU,VIBCAPTURE,CONFIRM,50,3000,20",
                "AT+HW=IMU,VIBCAPTURE,CONFIRM,50,3000,20,COMPACT",
            ),
            config.at_timeouts.vibcapture_timeout_s(),
        ),
        FlowStep(
            "PPG reflect capture",
            _capture_command(
                config,
                "AT+HW=PPG,CAPTURE,CONFIRM,REFLECT,3000,50",
                "AT+HW=PPG,CAPTURE,CONFIRM,REFLECT,3000,50,COMPACT",
            ),
            config.at_timeouts.ppg_capture_timeout_s(),
        ),
    ]


def run_half_machine(
    client: ATClient,
    config: WorkstationConfig,
    sn: str,
    token: str,
    record: RecordSink,
    progress: ProgressCallback,
    sn_enabled: bool = True,
    before_step: StepPromptCallback | None = None,
    skip_momo: bool = False,
    start_index: int = 1,
) -> FlowOutcome:
    if _missing_required_token(config, sn_enabled, token):
        return FlowOutcome(False, "NG", "missing factory token", [])
    if sn_enabled:
        ok, reason = config.validate_sn(sn)
        if not ok:
            return FlowOutcome(False, "NG", reason, [])
    steps = [
        FlowStep("AT probe", "AT"),
        FlowStep("Read version", "AT+VER?"),
    ]
    if token:
        steps.append(FlowStep("Factory unlock", f"AT+FACTORY=UNLOCK,{token}", config.at_timeouts.unlock_s))
    if sn_enabled:
        steps.extend(
            [
                FlowStep("Write SN", f"AT+SN={sn}"),
                FlowStep("Read SN", "AT+SN?"),
            ]
        )
    steps.extend(
        [
            FlowStep("Power path", "AT+HW=POWER"),
            *_half_chip_communication_steps(config),
            FlowStep(
                "PPG dark capture",
                _capture_command(
                    config,
                    "AT+HW=PPG,CAPTURE,CONFIRM,DARK,1000,100",
                    "AT+HW=PPG,CAPTURE,CONFIRM,DARK,1000,100,COMPACT",
                ),
                config.at_timeouts.ppg_capture_s,
            ),
            FlowStep("Touch ISR", "AT+HW=TOUCH,ISR,CONFIRM", config.at_timeouts.hw_short_s),
        ]
    )
    if token:
        steps.append(FlowStep("Factory lock", "AT+FACTORY=LOCK", config.at_timeouts.default_s, required=False))
    steps = _filter_optional_steps(steps, skip_momo)
    step_context = StepContext(start_index)
    cap_ok, cap_result = _check_factory_capability(client, record, "half", progress, step_context)
    if not cap_ok:
        return FlowOutcome(False, "NG", "factory AT not available", [cap_result])
    outcome = _run_steps(client, config, steps, progress, record, "half", before_step=before_step, step_context=step_context)
    outcome = _with_factory_lock_cleanup(client, config, record, "half", progress, step_context, outcome)
    if sn_enabled:
        sn_ok, sn_result = _check_sn_source(client, record, "half", progress, step_context)
        if not sn_ok:
            message = "SN persistence check failed"
            if outcome.result != "PASS":
                message = f"{outcome.message}; {message}"
            outcome = FlowOutcome(False, "NG", message, outcome.results + [sn_result])
    return outcome


def run_full_machine(
    client: ATClient,
    config: WorkstationConfig,
    sn: str,
    token: str,
    record: RecordSink,
    progress: ProgressCallback,
    sn_enabled: bool = True,
    before_step: StepPromptCallback | None = None,
    skip_momo: bool = False,
    start_index: int = 1,
) -> FlowOutcome:
    if _missing_required_token(config, sn_enabled, token):
        return FlowOutcome(False, "NG", "missing factory token", [])
    if sn_enabled:
        ok, reason = config.validate_sn(sn)
        if not ok:
            return FlowOutcome(False, "NG", reason, [])
    steps = [
        FlowStep("AT probe", "AT"),
        FlowStep("Read version", "AT+VER?"),
        FlowStep("Read OTA busy", "AT+OTABUSY?"),
    ]
    if sn_enabled:
        steps.insert(2, FlowStep("Read SN", "AT+SN?", validator=_sn_match_validator(sn)))
    step_context = StepContext(start_index)
    cap_ok, cap_result = _check_factory_capability(client, record, "full", progress, step_context)
    if not cap_ok:
        return FlowOutcome(False, "NG", "factory AT not available", [cap_result])
    if config.ota_enabled:
        outcome = _run_steps(client, config, steps, progress, record, "full", before_step=before_step, step_context=step_context)
        if not outcome.ok:
            return outcome
        ota_outcome = _run_ota_phase(client, config, record, progress, step_context)
        if not ota_outcome.ok:
            return FlowOutcome(
                ota_outcome.ok,
                ota_outcome.result,
                f"OTA phase failed: {ota_outcome.message}",
                ota_outcome.results,
            )
        post_steps = []
        if token:
            post_steps.append(FlowStep("Factory unlock", f"AT+FACTORY=UNLOCK,{token}", config.at_timeouts.unlock_s))
        post_steps.extend(_full_capture_steps(config))
        if token:
            post_steps.append(FlowStep("Factory lock", "AT+FACTORY=LOCK", config.at_timeouts.default_s, required=False))
        post_steps = _filter_optional_steps(post_steps, skip_momo)
        final = _run_steps(client, config, post_steps, progress, record, "full", before_step=before_step, step_context=step_context)
        final = _with_factory_lock_cleanup(client, config, record, "full", progress, step_context, final)
        return final
    if token:
        steps.append(FlowStep("Factory unlock", f"AT+FACTORY=UNLOCK,{token}", config.at_timeouts.unlock_s))
    steps += _full_capture_steps(config)
    if token:
        steps.append(FlowStep("Factory lock", "AT+FACTORY=LOCK", config.at_timeouts.default_s, required=False))
    steps = _filter_optional_steps(steps, skip_momo)
    outcome = _run_steps(client, config, steps, progress, record, "full", before_step=before_step, step_context=step_context)
    outcome = _with_factory_lock_cleanup(client, config, record, "full", progress, step_context, outcome)
    return outcome


def _run_ota_phase(
    client: ATClient,
    config: WorkstationConfig,
    record: RecordSink,
    progress: ProgressCallback,
    step_context: StepContext,
) -> FlowOutcome:
    from .ota_runner import build_ota_command, run_ota
    from .transport_ble import BLENusTransport

    transport = client._transport
    is_ble = isinstance(transport, BLENusTransport)
    if not is_ble:
        record.log_item("full", "OTA transport check", "transport", "NG", 0, "OTA requires BLE transport", "")
        return FlowOutcome(False, "NG", "OTA requires BLE transport", [])

    ble_name = transport.name
    ble_address = transport.address
    ble_scan_timeout = transport.scan_timeout_s
    ble_backend_kwargs = getattr(transport, "dongle_kwargs", {})

    results: list[CommandResult] = []

    idx = step_context.take()
    record.start_step(idx, "OTA version before", "AT+VER?")
    progress(idx, "OTA version before", "RUN", "AT+VER?")
    ver_before = client.send_command("AT+VER?", 8.0)
    results.append(ver_before)
    record.log_item("full", "OTA version before", "AT+VER?", "PASS" if ver_before.ok else "NG",
                    int(ver_before.elapsed_s * 1000), "", " ; ".join(ver_before.lines[-3:]))
    progress(idx, "OTA version before", "PASS" if ver_before.ok else "NG", " ; ".join(ver_before.lines[-2:]))

    idx = step_context.take()
    record.start_step(idx, "OTA busy check", "AT+OTABUSY?")
    progress(idx, "OTA busy check", "RUN", "AT+OTABUSY?")
    ota_busy = client.send_command("AT+OTABUSY?", 5.0)
    results.append(ota_busy)
    busy_text = _joined_lines(ota_busy)
    busy_locked = _ota_state_locked(busy_text)
    record.log_item("full", "OTA busy check", "AT+OTABUSY?", "PASS" if ota_busy.ok and not busy_locked else "NG",
                    int(ota_busy.elapsed_s * 1000),
                    "ota busy locked" if busy_locked else "", " ; ".join(ota_busy.lines[-3:]))
    progress(idx, "OTA busy check", "PASS" if ota_busy.ok and not busy_locked else "NG", " ; ".join(ota_busy.lines[-2:]))
    if busy_locked:
        return FlowOutcome(False, "NG", "OTA busy locked (factory active)", results)

    image_path = Path(config.ota_image_path)
    idx = step_context.take()
    record.start_step(idx, "OTA image check", str(image_path))
    if not image_path.exists():
        record.log_item("full", "OTA image check", str(image_path), "NG", 0, "image not found", "")
        progress(idx, "OTA image check", "NG", f"not found: {image_path}")
        return FlowOutcome(False, "NG", f"OTA image not found: {image_path}", results)
    record.log_item("full", "OTA image check", str(image_path), "PASS", 0, "", f"size={image_path.stat().st_size}")
    progress(idx, "OTA image check", "PASS", f"size={image_path.stat().st_size}")

    idx = step_context.take()
    record.start_step(idx, "OTA disconnect NUS", "AT+RST + close")
    progress(idx, "OTA disconnect NUS", "RUN", "requesting reboot and closing BLE NUS")
    reset_result = client.send_command("AT+RST", 4.0)
    results.append(reset_result)
    if not reset_result.ok:
        detail = " ; ".join(reset_result.lines[-3:])
        record.log_item("full", "OTA disconnect NUS", "AT+RST + close", "NG",
                        int(reset_result.elapsed_s * 1000), "device did not accept reboot", detail)
        progress(idx, "OTA disconnect NUS", "NG", detail or "device did not accept reboot")
        return FlowOutcome(False, "NG", "OTA handoff reboot failed", results)
    client.close()
    closed = client.wait_closed(5.0)
    record.log_item(
        "full",
        "OTA disconnect NUS",
        "AT+RST + close",
        "PASS" if closed else "NG",
        int(reset_result.elapsed_s * 1000),
        "" if closed else "BLE connection thread did not exit within 5 seconds",
        "device reboot accepted; nus closed for smp upload" if closed else "close timeout",
    )
    if not closed:
        progress(idx, "OTA disconnect NUS", "NG", "BLE connection thread did not exit within 5 seconds")
        return FlowOutcome(False, "NG", "OTA BLE connection release timed out", results)
    progress(idx, "OTA disconnect NUS", "PASS", "device reboot accepted; BLE session closed")

    time.sleep(5.0)

    ota_command = build_ota_command(config, ble_address)
    ota_script_name = ota_command.script_name
    idx = step_context.take()
    record.start_step(idx, "OTA upload", f"{ota_script_name} {image_path.name}")
    progress(idx, "OTA upload", "RUN", f"{image_path.name}")
    record.log_item("full", "OTA upload", f"{ota_script_name} {image_path.name}", "RUN", 0, "", "uploading")
    ota_lines: list[str] = []

    def _ota_capture(direction: str, line: str) -> None:
        record.log_at(direction, line)
        if direction == "OTA":
            ota_lines.append(line)

    t0 = time.monotonic()
    try:
        exit_code = run_ota(config, ble_address, _ota_capture)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record.log_item("full", "OTA upload", ota_script_name, "NG", elapsed_ms, str(exc), "")
        progress(idx, "OTA upload", "NG", str(exc))
        reconnect_index = step_context.take()
        _reconnect_nus(client, ble_name, ble_address, ble_scan_timeout, record, progress, reconnect_index, probe=False, ble_backend_kwargs=ble_backend_kwargs)
        return FlowOutcome(False, "NG", f"OTA upload exception: {exc}", results)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    ota_output = " ".join(ota_lines)
    same_hash = "matches the active image" in ota_output or "same image hash" in ota_output.lower()
    if exit_code == 0:
        ota_ok = True
        ota_reason = ""
    elif same_hash:
        ota_ok = True
        ota_reason = "same-hash upload confirmed; MCUboot rejected identical image swap (expected)"
    else:
        ota_ok = False
        ota_reason = f"exit_code={exit_code}"
    record.log_item("full", "OTA upload", ota_script_name, "PASS" if ota_ok else "NG", elapsed_ms,
                    ota_reason, f"exit_code={exit_code}")
    progress(idx, "OTA upload", "PASS" if ota_ok else "NG", f"exit_code={exit_code} elapsed={elapsed_ms}ms {ota_reason}")
    if not ota_ok:
        idx = step_context.take()
        _reconnect_nus(client, ble_name, ble_address, ble_scan_timeout, record, progress, idx, probe=False, ble_backend_kwargs=ble_backend_kwargs)
        return FlowOutcome(False, "NG", f"OTA upload failed exit_code={exit_code}", results)

    if same_hash:
        idx = step_context.take()
        reconnect_ok, probe_result, reconnect_reason = _reconnect_nus(
            client, ble_name, ble_address, ble_scan_timeout, record, progress, idx, probe=True, ble_backend_kwargs=ble_backend_kwargs
        )
        if probe_result is not None:
            results.append(probe_result)
        if not reconnect_ok:
            return FlowOutcome(False, "NG", f"OTA reconnect failed: {reconnect_reason}", results)

        idx = step_context.take()
        record.start_step(idx, "OTA state check", "AT+OTA?")
        progress(idx, "OTA state check", "RUN", "AT+OTA?")
        ota_state = client.send_command("AT+OTA?", 5.0)
        results.append(ota_state)
        ota_state_text = _joined_lines(ota_state)
        ota_locked = _ota_state_locked(ota_state_text)
        record.log_item("full", "OTA state check", "AT+OTA?", "WARN" if ota_locked else ("PASS" if ota_state.ok else "NG"),
                        int(ota_state.elapsed_s * 1000), "locked" if ota_locked else "",
                        " ; ".join(ota_state.lines[-3:]))
        progress(idx, "OTA state check", "WARN" if ota_locked else ("PASS" if ota_state.ok else "NG"),
                 " ; ".join(ota_state.lines[-2:]))

        idx = step_context.take()
        record.start_step(idx, "OTA busy after same-hash", "AT+OTABUSY?")
        progress(idx, "OTA busy after same-hash", "RUN", "AT+OTABUSY?")
        ota_busy_after = client.send_command("AT+OTABUSY?", 5.0)
        results.append(ota_busy_after)
        ota_busy_text = _joined_lines(ota_busy_after)
        ota_clear = ota_busy_after.ok and _ota_state_clear(ota_busy_text)
        record.log_item("full", "OTA busy after same-hash", "AT+OTABUSY?", "PASS" if ota_clear else "WARN",
                        int(ota_busy_after.elapsed_s * 1000),
                        "" if ota_clear else "same-hash does not validate swap", " ; ".join(ota_busy_after.lines[-3:]))
        progress(idx, "OTA busy after same-hash", "PASS" if ota_clear else "WARN", " ; ".join(ota_busy_after.lines[-2:]))
        return FlowOutcome(
            False,
            "PENDING-HW",
            "OTA same-hash upload verified only; different-image OTA required for full flow",
            results,
        )

    idx = step_context.take()
    record.start_step(idx, "OTA reboot wait", "sleep")
    progress(idx, "OTA reboot wait", "RUN", f"waiting {config.ota_reboot_wait_s}s")
    time.sleep(config.ota_reboot_wait_s)
    record.log_item("full", "OTA reboot wait", "sleep", "PASS", int(config.ota_reboot_wait_s * 1000), "", "")

    idx = step_context.take()
    reconnect_ok, probe, reconnect_reason = _reconnect_nus(
        client, ble_name, ble_address, ble_scan_timeout, record, progress, idx, probe=True, ble_backend_kwargs=ble_backend_kwargs
    )
    if probe is not None:
        results.append(probe)
    if not reconnect_ok:
        return FlowOutcome(False, "NG", f"OTA reconnect failed: {reconnect_reason}", results)

    idx = step_context.take()
    record.start_step(idx, "OTA busy after reboot", "AT+OTABUSY?")
    progress(idx, "OTA busy after reboot", "RUN", "AT+OTABUSY?")
    ota_busy_after = client.send_command("AT+OTABUSY?", 8.0)
    results.append(ota_busy_after)
    ota_busy_after_text = _joined_lines(ota_busy_after)
    ota_clear = ota_busy_after.ok and _ota_state_clear(ota_busy_after_text)
    record.log_item("full", "OTA busy after reboot", "AT+OTABUSY?", "PASS" if ota_clear else "NG",
                    int(ota_busy_after.elapsed_s * 1000),
                    "" if ota_clear else "OTA lock not clear", " ; ".join(ota_busy_after.lines[-3:]))
    progress(idx, "OTA busy after reboot", "PASS" if ota_clear else "NG", " ; ".join(ota_busy_after.lines[-2:]))
    if not ota_clear:
        return FlowOutcome(False, "NG", "OTA lock did not clear after reconnect", results)

    idx = step_context.take()
    record.start_step(idx, "OTA version after", "AT+VER?")
    progress(idx, "OTA version after", "RUN", "AT+VER?")
    ver_after = client.send_command("AT+VER?", 8.0)
    results.append(ver_after)
    record.log_item("full", "OTA version after", "AT+VER?", "PASS" if ver_after.ok else "NG",
                    int(ver_after.elapsed_s * 1000), "", " ; ".join(ver_after.lines[-3:]))
    progress(idx, "OTA version after", "PASS" if ver_after.ok else "NG", " ; ".join(ver_after.lines[-2:]))

    return FlowOutcome(True, "PASS", "OTA phase completed", results)


def _check_sn_source(
    client: ATClient,
    record: RecordSink,
    station_type: str,
    progress: ProgressCallback,
    step_context: StepContext,
) -> tuple[bool, CommandResult]:
    index = step_context.take()
    record.start_step(index, "SN persistence check", "AT+SN?")
    progress(index, "SN persistence check", "RUN", "AT+SN?")
    result = client.send_command("AT+SN?", 5.0)
    source = ""
    for line in result.lines:
        if "source=" in line:
            for part in line.split(","):
                part = part.strip()
                if part.startswith("source="):
                    source = part.split("=", 1)[1]
                    break
    sn_ok = result.ok and source == "lfs"
    detail = " ; ".join(result.lines[-3:])
    record.log_item(
        station_type,
        "SN persistence check",
        "AT+SN?",
        "PASS" if sn_ok else "NG",
        int(result.elapsed_s * 1000),
        "" if sn_ok else f"source={source or 'unknown'} (expected lfs)",
        detail,
    )
    progress(index, "SN persistence check", "PASS" if sn_ok else "NG", detail)
    return sn_ok, result
