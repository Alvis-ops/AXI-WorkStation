# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['serial.tools.list_ports', 'bleak.backends.winrt.client', 'bleak.backends.winrt.scanner']
datas += [
    ('tools\\factory_workstation\\nrf_dongle_scan.js', 'factory_workstation'),
    ('tools\\factory_workstation\\nrf_dongle_nus.js', 'factory_workstation'),
    ('tools\\factory_workstation\\flash_selected_image.ps1', 'factory_workstation'),
]
hiddenimports += collect_submodules('bleak')
hiddenimports += collect_submodules('smpclient')
tmp_ret = collect_all('ttkbootstrap')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


gui_a = Analysis(
    ['tools\\factory_workstation\\app.py'],
    pathex=['tools'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
gui_pyz = PYZ(gui_a.pure)

gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name='Axi Factory Workstation',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli_a = Analysis(
    ['tools\\factory_workstation\\cli.py'],
    pathex=['tools'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name='Axi Factory Workstation CLI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

ota_a = Analysis(
    ['..\\axi-p1-embeded\\tools\\ota_smp_ble.py'],
    pathex=['..\\axi-p1-embeded\\tools'],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
ota_pyz = PYZ(ota_a.pure)
ota_exe = EXE(
    ota_pyz,
    ota_a.scripts,
    [],
    exclude_binaries=True,
    name='Axi OTA Helper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    gui_exe,
    cli_exe,
    ota_exe,
    gui_a.binaries,
    gui_a.datas,
    cli_a.binaries,
    cli_a.datas,
    ota_a.binaries,
    ota_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Axi Factory Workstation',
)
