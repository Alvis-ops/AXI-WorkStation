# 构建与安装说明

本文说明如何从源码构建 AXI 半机/整机上位机、独立 OTA 上位机、裸板上位机，以及如何制作 Win10 x64 离线 USB 安装包。

## 1. 准备构建环境

推荐在 Windows 10/11 x64 构建机上执行。

```powershell
git clone https://github.com/Axi-labs/AXI-WORKSTATION.git
cd AXI-WORKSTATION

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r tools\requirements-workstation.txt
```

需要准备：

- Python 3.10 或更高版本。
- PyInstaller，已包含在 `tools/requirements-workstation.txt`。
- 半机/整机 OTA Helper 的源码：当前 spec 从相邻固件仓库 `..\axi-p1-embeded\tools\ota_smp_ble.py` 读取。
- 制作完整离线包时所需的 VC++ x64、Nordic Command Line Tools、nRF Connect BLE 和固件文件。

建议工作区布局：

```text
workspace/
├─ AXI-WORKSTATION/
└─ axi-p1-embeded/
   ├─ tools/ota_smp_ble.py
   └─ build_ondemand/
      ├─ merged.hex
      └─ axi-p1-embeded/zephyr/zephyr.signed.bin
```

## 2. 构建半机/整机上位机

在 `AXI-WORKSTATION` 根目录执行：

```powershell
python -m PyInstaller --clean --noconfirm "Axi Factory Workstation.spec"
```

输出目录：

```text
dist/Axi Factory Workstation/
```

目录内应至少包含：

- `Axi Factory Workstation.exe`
- `Axi Factory Workstation CLI.exe`
- `Axi OTA Helper.exe`
- `factory_workstation/flash_selected_image.ps1`
- `factory_workstation/nrf_dongle_scan.js`
- `factory_workstation/nrf_dongle_nus.js`

如提示找不到 `..\axi-p1-embeded\tools\ota_smp_ble.py`，请按上一节准备相邻固件仓库后重新构建。

## 3. 构建裸板上位机

```powershell
python -m PyInstaller --clean --noconfirm "Axi Bare Board Workstation.spec"
```

输出目录：

```text
dist/Axi Bare Board Workstation/
```

## 3.1 构建独立 OTA 上位机

```powershell
python -m PyInstaller --clean --noconfirm "Axi OTA Workstation.spec"
```

输出目录：

```text
dist/Axi OTA Workstation/
```

目录内应至少包含 `Axi OTA Workstation.exe`、`Axi OTA BLE Helper.exe` 和 `Axi OTA Dongle Helper.exe`。BLE Helper 的源码同样从相邻固件仓库 `..\axi-p1-embeded\tools\ota_smp_ble.py` 读取。

## 4. 本机便携运行

PyInstaller 构建完成后，可以直接运行程序目录中的 EXE。首次运行前将配置模板复制到程序目录并按本机修改：

```powershell
Copy-Item tools\factory_workstation\config.json.example "dist\Axi Factory Workstation\config.json"
```

不要把真实 `.env` 或 `config.json` 放回 Git 仓库。正式安装包的构建脚本会自动生成不含凭据的 `.env.template`。

## 5. 制作半机/整机 Win10 离线 USB 包

先完成第 2 节，确保 `dist\Axi Factory Workstation` 存在。然后准备以下输入：

| 输入 | 用途 |
| --- | --- |
| `merged.hex` | J-Link/nrfjprog 芯片烧录固件 |
| `zephyr.signed.bin` | BLE SMP OTA 固件 |
| VC++ 2015-2022 x64 安装器 | 目标机运行库 |
| Nordic Command Line Tools 安装器 | `nrfjprog` 和兼容 J-Link 环境 |
| nRF Connect BLE 程序目录 | nRF Dongle BLE 后端 |

构建示例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\build_offline_usb_win10.ps1 `
  -AppDir ".\dist\Axi Factory Workstation" `
  -FirmwareHexPath "D:\release\merged.hex" `
  -FirmwareOtaPath "D:\release\zephyr.signed.bin" `
  -VcRedistPath "D:\deps\vc_redist.x64.exe" `
  -NordicCliInstallerPath "D:\deps\nrf-command-line-tools-installer.exe" `
  -NrfConnectBleDir "$env:LOCALAPPDATA\Programs\nrfconnect-bluetooth-low-energy" `
  -PackageRevision "r1"
```

如果允许构建机联网下载 VC++ 运行库，可使用 `-DownloadVcRedist` 代替 `-VcRedistPath`。

默认输出：

```text
dist/AxiFactoryWorkstation_win10_x64_offline_usb_<日期>_<修订号>/
```

完整包应包含：

```text
install_offline_win10.cmd
install_offline_win10.ps1
SHA256SUMS.txt
MANIFEST.json
app/
deps/
firmware/
shared/
```

构建脚本会在控制台和 `MANIFEST.json` 中列出缺失依赖。存在缺失项的目录只能用于检查结构，不能作为完整离线交付包。

## 6. 在产线电脑安装

将整个离线包目录复制到 U 盘或目标电脑，双击：

```text
install_offline_win10.cmd
```

也可以从 PowerShell 指定安装目录：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_offline_win10.ps1 `
  -InstallRoot "D:\Axi\FactoryWorkstation"
```

安装脚本会：

1. 校验 `SHA256SUMS.txt`。
2. 安装或复用 VC++、Nordic CLI/J-Link 和 nRF Connect BLE。
3. 安装上位机程序。
4. 复制默认烧录固件和 OTA 固件。
5. 写入不含凭据的本机配置。
6. 检查 EXE、固件、BLE 后端及 `nrfjprog` 环境。

安装后首次启动需要工程师设置密码并配置工厂令牌。每台工位还需确认 UART COM、Dongle COM、J-Link ID、Device、Line、MES URL 和固件路径。

## 7. 制作裸板离线包

先构建裸板程序，然后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\bare_board\build_offline_usb_win10.ps1 `
  -AppDir ".\dist\Axi Bare Board Workstation" `
  -FirmwareHexPath "D:\release\bare_board_test.hex" `
  -VcRedistPath "D:\deps\vc_redist.x64.exe" `
  -NordicCliInstallerPath "D:\deps\nrf-command-line-tools-installer.exe" `
  -PackageRevision "r1"
```

在目标电脑运行生成目录中的 `install_offline_win10.cmd`。

## 8. 验证

构建前运行无硬件测试：

```powershell
python tools\factory_workstation\smoke_p1_0h.py
python tools\factory_workstation\smoke_mes.py
python -m tools.bare_board_workstation.smoke_bare_board
python -m tools.ota_workstation.smoke_ota
git diff --check
```

打包后至少检查：

```powershell
Test-Path "dist\Axi Factory Workstation\Axi Factory Workstation.exe"
Test-Path "dist\Axi Factory Workstation\Axi Factory Workstation CLI.exe"
Test-Path "dist\Axi Factory Workstation\Axi OTA Helper.exe"
Test-Path "dist\Axi OTA Workstation\Axi OTA Workstation.exe"
Test-Path "dist\Axi OTA Workstation\Axi OTA BLE Helper.exe"
Test-Path "dist\Axi OTA Workstation\Axi OTA Dongle Helper.exe"
```

完整交付还需在干净的 Win10 x64 电脑上完成安装测试，并分别验证 GUI 启动、UART、BLE、J-Link、OTA、MES 和记录路径。

## 9. 不进入 Git 的文件

- `build/`、`dist/`
- `factory_records/`、`bare_board_records/`
- `tools/**/config.json`
- `tools/factory_workstation/.env`
- 固件镜像、依赖安装器、EXE、ZIP 和现场日志

发布前使用 `git status --short` 检查暂存内容，只提交源码、模板和文档。
