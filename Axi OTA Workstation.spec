# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [
    ('tools\\factory_workstation\\nrf_dongle_scan.js', 'factory_workstation'),
    ('tools\\factory_workstation\\nrf_dongle_nus.js', 'factory_workstation'),
]
binaries = []
hiddenimports = [
    'serial.tools.list_ports',
    'bleak.backends.winrt.client',
    'bleak.backends.winrt.scanner',
]
hiddenimports += collect_submodules('bleak')
hiddenimports += collect_submodules('smpclient')
tmp_ret = collect_all('ttkbootstrap')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]


gui_a = Analysis(
    ['tools\\ota_workstation\\app.py'],
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
    name='Axi OTA Workstation',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)


ble_a = Analysis(
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
ble_pyz = PYZ(ble_a.pure)
ble_exe = EXE(
    ble_pyz,
    ble_a.scripts,
    [],
    exclude_binaries=True,
    name='Axi OTA BLE Helper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)


dongle_a = Analysis(
    ['tools\\ota_smp_dongle.py'],
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
dongle_pyz = PYZ(dongle_a.pure)
dongle_exe = EXE(
    dongle_pyz,
    dongle_a.scripts,
    [],
    exclude_binaries=True,
    name='Axi OTA Dongle Helper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)


coll = COLLECT(
    gui_exe,
    ble_exe,
    dongle_exe,
    gui_a.binaries,
    gui_a.datas,
    ble_a.binaries,
    ble_a.datas,
    dongle_a.binaries,
    dongle_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Axi OTA Workstation',
)
