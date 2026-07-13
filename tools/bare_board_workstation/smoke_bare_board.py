from __future__ import annotations

import sys
import tempfile
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bare_board_workstation.config import BareBoardConfig
    from bare_board_workstation.flash_runner import _parse_probe_ids, build_flash_command, detect_jlink_probes
    from bare_board_workstation.flow import make_simulated_flash_runner, make_simulated_serial_runner, run_bare_board_test
else:
    from .config import BareBoardConfig
    from .flash_runner import _parse_probe_ids, build_flash_command, detect_jlink_probes
    from .flow import make_simulated_flash_runner, make_simulated_serial_runner, run_bare_board_test


def _expect_error(label: str, func) -> None:
    try:
        func()
    except Exception:
        return
    raise AssertionError(f"expected error: {label}")


def test_sn_rule() -> None:
    config = BareBoardConfig()
    ok, _reason = config.validate_sn("AXI_001")
    assert ok
    ok, _reason = config.validate_sn("")
    assert not ok
    ok, _reason = config.validate_sn("bad space")
    assert not ok


def test_flash_precheck() -> None:
    config = BareBoardConfig(flash_image_path="")
    _expect_error("empty flash image", lambda: build_flash_command(config))
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        config.flash_image_path = str(temp_dir)
        _expect_error("directory flash image", lambda: build_flash_command(config))
        image = temp_dir / "image.hex"
        image.write_text(":00000001FF\n", encoding="ascii")
        jlink_dll = temp_dir / "JLinkARM.dll"
        jlink_dll.write_bytes(b"test")
        config.flash_image_path = str(image)
        config.jlink_dll_path = str(jlink_dll)
        command = build_flash_command(config)
        assert "--program" in command.argv
        assert str(image) in command.argv
        assert command.argv[command.argv.index("--jdll") + 1] == str(jlink_dll)


def test_detect_jlink_script_backend() -> None:
    config = BareBoardConfig(flash_backend="script", flash_script_path="flash.ps1")
    result = detect_jlink_probes(config)
    assert result.ok
    assert result.level == "WARN"


def test_probe_id_parser_ignores_jlink_errors() -> None:
    output = "\n".join(
        [
            "[error] [SeggerBackend] - JLinkARM.dll reported error -256 at line 736.",
            "[error] [SeggerBackend] - JLinkARM.dll reported error -256 at line 629.",
            "69730371",
        ]
    )
    assert _parse_probe_ids(output) == ["69730371"]


def test_flow_dry_run_record() -> None:
    with tempfile.TemporaryDirectory() as temp:
        config = BareBoardConfig(
            records_root=temp,
            flash_after_wait_s=0.0,
            serial_timeout_s=2.0,
            serial_open_wait_s=0.0,
            start_prompt_patterns=["re:\\[DRVTEST\\]\\[WAIT\\]"],
            start_prompt_timeout_s=1.0,
            test_start_command="AT+DRVTEST",
            pass_patterns=["re:\\[DRVTEST\\]\\[FINAL\\].*overall=PASS"],
            end_patterns=["re:\\[DRVTEST\\]\\[FINAL\\]"],
        )
        outcome = run_bare_board_test(
            config,
            "SN001",
            flash_runner=make_simulated_flash_runner(),
            serial_runner=make_simulated_serial_runner([
                "BOOT",
                "[DRVTEST][WAIT]",
                "[DRVTEST][FINAL] overall=PASS",
            ]),
        )
        assert outcome.ok, outcome
        record = Path(outcome.record_path)
        assert record.exists()
        text = record.read_text(encoding="utf-8")
        assert "sn=SN001" in text
        assert "[SERIAL_RX] [DRVTEST][WAIT]" in text
        assert "[SERIAL_RX] [DRVTEST][FINAL] overall=PASS" in text
        assert "result=PASS" in text


def test_flow_without_record() -> None:
    with tempfile.TemporaryDirectory() as temp:
        config = BareBoardConfig(
            records_root=temp,
            flash_after_wait_s=0.0,
            serial_timeout_s=2.0,
            serial_open_wait_s=0.0,
            start_prompt_patterns=["re:\\[DRVTEST\\]\\[WAIT\\]"],
            start_prompt_timeout_s=1.0,
            test_start_command="AT+DRVTEST",
            pass_patterns=["re:\\[DRVTEST\\]\\[FINAL\\].*overall=PASS"],
            end_patterns=["re:\\[DRVTEST\\]\\[FINAL\\]"],
            sn_record_enabled=False,
        )
        outcome = run_bare_board_test(
            config,
            "",
            flash_runner=make_simulated_flash_runner(),
            serial_runner=make_simulated_serial_runner([
                "[DRVTEST][WAIT]",
                "[DRVTEST][FINAL] overall=PASS",
            ]),
            record_enabled=False,
        )
        assert outcome.ok, outcome
        assert not outcome.record_path


def test_flow_sends_at_without_wait_prompt() -> None:
    with tempfile.TemporaryDirectory() as temp:
        config = BareBoardConfig(
            records_root=temp,
            flash_after_wait_s=0.0,
            serial_timeout_s=2.0,
            serial_open_wait_s=0.0,
            start_prompt_patterns=["re:\\[DRVTEST\\]\\[WAIT\\]"],
            start_prompt_timeout_s=0.2,
            require_start_prompt=False,
            test_start_command="AT+DRVTEST",
            pass_patterns=["re:\\[DRVTEST\\]\\[FINAL\\].*overall=PASS"],
            end_patterns=["re:\\[DRVTEST\\]\\[FINAL\\]"],
        )
        outcome = run_bare_board_test(
            config,
            "SN002",
            flash_runner=make_simulated_flash_runner(),
            serial_runner=make_simulated_serial_runner([
                "[DRVTEST][START]",
                "[DRVTEST][FINAL] overall=PASS",
            ]),
        )
        assert outcome.ok, outcome
        text = Path(outcome.record_path).read_text(encoding="utf-8")
        assert "[SERIAL_TX] AT+DRVTEST" in text
        assert "[SERIAL] WARN:" in text


def test_flow_dry_run_fail() -> None:
    with tempfile.TemporaryDirectory() as temp:
        config = BareBoardConfig(
            records_root=temp,
            flash_after_wait_s=0.0,
            serial_timeout_s=2.0,
            serial_open_wait_s=0.0,
            test_start_command="AT+DRVTEST",
            pass_patterns=[r"re:\[DRVTEST\]\[FINAL\].*overall=PASS"],
            fail_patterns=[
                r"re:\[DRVTEST\]\[FINAL\].*overall=FAIL",
                r"re:\[DRVTEST\]\[ABORT\]",
            ],
            end_patterns=[r"re:\[DRVTEST\]\[FINAL\]"],
        )
        outcome = run_bare_board_test(
            config,
            "SN_FAIL",
            flash_runner=make_simulated_flash_runner(),
            serial_runner=make_simulated_serial_runner([
                "[DRVTEST][START]",
                "[CASE] name=aw96105.probe status=FAIL",
                "[DRVTEST][FINAL] run=1 overall=FAIL records=3 active=0x0002 elapsed_ms=800",
            ]),
        )
        assert not outcome.ok, outcome
        assert outcome.result == "NG", outcome
        text = Path(outcome.record_path).read_text(encoding="utf-8")
        assert "result=NG" in text
        assert "[DRVTEST][FINAL] run=1 overall=FAIL" in text


def test_flow_dry_run_abort() -> None:
    with tempfile.TemporaryDirectory() as temp:
        config = BareBoardConfig(
            records_root=temp,
            flash_after_wait_s=0.0,
            serial_timeout_s=2.0,
            serial_open_wait_s=0.0,
            test_start_command="AT+DRVTEST",
            pass_patterns=[r"re:\[DRVTEST\]\[FINAL\].*overall=PASS"],
            fail_patterns=[
                r"re:\[DRVTEST\]\[FINAL\].*overall=FAIL",
                r"re:\[DRVTEST\]\[ABORT\]",
            ],
            end_patterns=[r"re:\[DRVTEST\]\[FINAL\]"],
        )
        outcome = run_bare_board_test(
            config,
            "SN_ABORT",
            flash_runner=make_simulated_flash_runner(),
            serial_runner=make_simulated_serial_runner([
                "[DRVTEST][ABORT] command not received",
            ]),
        )
        assert not outcome.ok, outcome
        assert "fail pattern matched" in outcome.message


def main() -> None:
    test_sn_rule()
    test_flash_precheck()
    test_detect_jlink_script_backend()
    test_probe_id_parser_ignores_jlink_errors()
    test_flow_dry_run_record()
    test_flow_dry_run_fail()
    test_flow_dry_run_abort()
    test_flow_without_record()
    test_flow_sends_at_without_wait_prompt()
    print("bare_board_smoke PASS")


if __name__ == "__main__":
    main()
