from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bare_board_workstation.config import CONFIG_PATH, load_config
    from bare_board_workstation.flow import make_simulated_flash_runner, make_simulated_serial_runner, run_bare_board_test
else:
    from .config import CONFIG_PATH, load_config
    from .flow import make_simulated_flash_runner, make_simulated_serial_runner, run_bare_board_test


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bare-board test workstation CLI")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="config JSON path")
    parser.add_argument("--sn", required=True, help="DUT serial number")
    parser.add_argument("--port", help="serial port, for example COM18")
    parser.add_argument("--baudrate", type=int, help="serial baudrate")
    parser.add_argument("--serial-timeout-s", type=float, help="serial log timeout seconds")
    parser.add_argument("--start-command", help="command sent after opening serial")
    parser.add_argument("--start-prompt", action="append", dest="start_prompt_patterns", help="start prompt pattern")
    parser.add_argument("--start-prompt-timeout-s", type=float, help="start prompt timeout seconds")
    parser.add_argument("--pass-pattern", action="append", dest="pass_patterns", help="PASS pattern")
    parser.add_argument("--fail-pattern", action="append", dest="fail_patterns", help="FAIL pattern")
    parser.add_argument("--end-pattern", action="append", dest="end_patterns", help="end pattern")
    parser.add_argument("--flash-image", help="hex image for SWD flashing")
    parser.add_argument("--flash-backend", choices=("nrfjprog", "script"), help="SWD flash backend")
    parser.add_argument("--flash-script", help="PowerShell flash script path for script backend")
    parser.add_argument("--nrfjprog-path", help="nrfjprog executable path; defaults to PATH lookup")
    parser.add_argument("--jlink-dll-path", help="JLinkARM.dll path passed to nrfjprog --jdll")
    parser.add_argument("--nrfjprog-family", help="nrfjprog --family value")
    parser.add_argument("--jlink-probe-id", help="J-Link serial number")
    parser.add_argument("--records-root", help="record output root")
    parser.add_argument("--dry-run", action="store_true", help="simulate flash and serial log without hardware")
    parser.add_argument(
        "--simulate-outcome",
        choices=("pass", "fail", "abort"),
        default="pass",
        help="dry-run serial outcome (default: pass)",
    )
    return parser.parse_args(argv)


def _apply_overrides(config, args: argparse.Namespace) -> None:
    if args.port:
        config.serial_port = args.port
    if args.baudrate:
        config.serial_baudrate = args.baudrate
    if args.serial_timeout_s is not None:
        config.serial_timeout_s = args.serial_timeout_s
    if args.start_command is not None:
        config.test_start_command = args.start_command
    if args.start_prompt_patterns is not None:
        config.start_prompt_patterns = args.start_prompt_patterns
    if args.start_prompt_timeout_s is not None:
        config.start_prompt_timeout_s = args.start_prompt_timeout_s
    if args.pass_patterns is not None:
        config.pass_patterns = args.pass_patterns
    if args.fail_patterns is not None:
        config.fail_patterns = args.fail_patterns
    if args.end_patterns is not None:
        config.end_patterns = args.end_patterns
    if args.flash_image:
        config.flash_image_path = args.flash_image
    if args.flash_backend:
        config.flash_backend = args.flash_backend
    if args.flash_script:
        config.flash_script_path = args.flash_script
    if args.nrfjprog_path:
        config.nrfjprog_path = args.nrfjprog_path
    if args.jlink_dll_path:
        config.jlink_dll_path = args.jlink_dll_path
    if args.nrfjprog_family:
        config.nrfjprog_family = args.nrfjprog_family
    if args.jlink_probe_id:
        config.jlink_probe_id = args.jlink_probe_id
    if args.records_root:
        config.records_root = args.records_root


def _simulated_serial_lines(outcome: str) -> list[str]:
    if outcome == "fail":
        return [
            "[DRVTEST][START]",
            "[CASE] name=power.rails status=PASS",
            "[CASE] name=lsm6ds3trc.probe status=FAIL err=-5",
            "[DRVTEST][FINAL] run=1 overall=FAIL records=2 active=0x0001 elapsed_ms=1200",
        ]
    if outcome == "abort":
        return [
            "[DRVTEST][WAIT] Send command to start: AT+DRVTEST",
            "[DRVTEST][ABORT] command not received",
        ]
    return [
        "[DRVTEST][START]",
        "[DRVTEST][FINAL] run=1 overall=PASS records=7 active=0x0000 elapsed_ms=3500",
    ]


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(Path(args.config))
    _apply_overrides(config, args)

    def line(direction: str, text: str) -> None:
        print(f"[{direction}] {text}", flush=True)

    def progress(step: str, status: str, detail: str) -> None:
        print(f"[STEP] {step}: {status} {detail}", flush=True)

    flash_runner = make_simulated_flash_runner() if args.dry_run else None
    serial_runner = (
        make_simulated_serial_runner(_simulated_serial_lines(args.simulate_outcome))
        if args.dry_run
        else None
    )
    outcome = run_bare_board_test(
        config,
        args.sn,
        line_callback=line,
        progress_callback=progress,
        flash_runner=flash_runner,
        serial_runner=serial_runner,
    )
    print(f"[RESULT] {outcome.result} {outcome.message}", flush=True)
    if outcome.record_path:
        print(f"[RECORD] {outcome.record_path}", flush=True)
    return 0 if outcome.ok else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
