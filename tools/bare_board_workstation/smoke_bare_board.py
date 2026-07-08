from __future__ import annotations

import sys
import tempfile
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bare_board_workstation.config import BareBoardConfig
    from bare_board_workstation.flash_runner import build_flash_command
    from bare_board_workstation.flow import make_simulated_flash_runner, make_simulated_serial_runner, run_bare_board_test
else:
    from .config import BareBoardConfig
    from .flash_runner import build_flash_command
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
        config.flash_image_path = str(image)
        command = build_flash_command(config)
        assert "--program" in command.argv
        assert str(image) in command.argv


def test_flow_dry_run_record() -> None:
    with tempfile.TemporaryDirectory() as temp:
        config = BareBoardConfig(
            records_root=temp,
            flash_after_wait_s=0.0,
            serial_timeout_s=2.0,
            serial_open_wait_s=0.0,
        )
        outcome = run_bare_board_test(
            config,
            "SN001",
            flash_runner=make_simulated_flash_runner(),
            serial_runner=make_simulated_serial_runner(["BOOT", "RUNNING", "LOG line 1", "RESULT:PASS"]),
        )
        assert outcome.ok, outcome
        record = Path(outcome.record_path)
        assert record.exists()
        text = record.read_text(encoding="utf-8")
        assert "sn=SN001" in text
        assert "[SERIAL_RX] RUNNING" in text
        assert "[SERIAL_RX] LOG line 1" in text
        assert "result=PASS" in text


def main() -> None:
    test_sn_rule()
    test_flash_precheck()
    test_flow_dry_run_record()
    print("bare_board_smoke PASS")


if __name__ == "__main__":
    main()
