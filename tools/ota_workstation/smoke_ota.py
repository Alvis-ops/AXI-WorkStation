from __future__ import annotations

import tempfile
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ota_workstation.config import OtaConfig, load_config, save_config
    from ota_workstation.ota_runner import build_ota_command, parse_progress
else:
    from .config import OtaConfig, load_config, save_config
    from .ota_runner import build_ota_command, parse_progress


def test_config_roundtrip(temp_dir: Path) -> None:
    image = temp_dir / "zephyr.signed.bin"
    image.write_bytes(b"test")
    path = temp_dir / "config.json"
    expected = OtaConfig(
        ble_backend="windows",
        ble_address="E3:A9:F3:49:97:A7",
        image_path=str(image),
        profile="balanced",
        reboot_wait_s=12.5,
    )
    save_config(expected, path)
    actual = load_config(path)
    assert actual.normalized_backend() == "windows"
    assert actual.ble_address == expected.ble_address
    assert actual.image_path == str(image)
    assert actual.reboot_wait_s == 12.5
    assert not actual.validate()


def test_command_selection(temp_dir: Path) -> None:
    image = temp_dir / "image.bin"
    image.write_bytes(b"image")
    bleak_helper = temp_dir / "bleak-helper.exe"
    dongle_helper = temp_dir / "dongle-helper.exe"
    bleak_helper.write_bytes(b"")
    dongle_helper.write_bytes(b"")

    config = OtaConfig(image_path=str(image), ble_address="AA:BB:CC:DD:EE:FF")
    dongle = build_ota_command(
        config,
        config.ble_address,
        windows_helper=bleak_helper,
        dongle_helper=dongle_helper,
    )
    assert dongle.argv[0] == str(dongle_helper)
    assert "--dongle-port" in dongle.argv
    assert "--verify-after-reset" in dongle.argv

    config.ble_backend = "windows"
    config.ble_pairing_enabled = True
    windows = build_ota_command(
        config,
        config.ble_address,
        windows_helper=bleak_helper,
        dongle_helper=dongle_helper,
    )
    assert windows.argv[0] == str(bleak_helper)
    assert "--pair" in windows.argv
    assert "--dongle-port" not in windows.argv


def test_progress_parser() -> None:
    assert parse_progress("  1024/2048 bytes (50.0%)") == 50.0
    assert parse_progress("Done in 2.0s") is None
    assert parse_progress("(120.0%)") == 100.0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="axi_ota_smoke_") as temp:
        temp_dir = Path(temp)
        test_config_roundtrip(temp_dir)
        test_command_selection(temp_dir)
        test_progress_parser()
    print("OTA workstation smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
