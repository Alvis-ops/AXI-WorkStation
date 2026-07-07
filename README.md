# AXI Factory Workstation

POC3A factory half/full machine host application. Exported from firmware repo axi-p1-embeded P1_0I baseline (2026-07-07) for standalone maintenance while Win7 adaptation proceeds in parallel.

## Baseline

- Firmware branch: merge/poc3a-current-20260626 @ e20d4ac (export includes uncommitted P1_0I workspace changes)
- Validation: DONGLE BLE full dry-run 3/3 PASS-HW; SN record run 1x PASS-HW; smoke_p1_0h.py PASS-SIM

## Layout

- tools/factory_workstation/ - Python app and nrf_dongle_*.js helpers
- tools/ota_smp_dongle.py - DONGLE BLE SMP OTA helper
- tools/requirements-workstation.txt - runtime/build dependencies
- Axi Factory Workstation.spec / Win7.spec - PyInstaller specs
- packaging/ - install notes and script templates

## Run from source

```powershell
cd tools
pip install -r requirements-workstation.txt
copy factory_workstation\config.json.example factory_workstation\config.json
copy factory_workstation\.env.example factory_workstation\.env
python factory_workstation\app.py
```

See tools/factory_workstation/README.md for CLI and smoke usage.

## Build

```powershell
pyinstaller "Axi Factory Workstation.spec"
pyinstaller "Axi Factory Workstation Win7.spec"
```

## Do not commit

- tools/factory_workstation/.env (tokens/passwords)
- tools/factory_workstation/config.json (machine-specific paths)
- dist/ and factory_records/
