# AXI Workstation

AXI P1 产线测试上位机源码仓库，包含：

- 半机/整机测试上位机：UART、BLE、SN、烧录、OTA、MES 上传和本地测试记录。
- 独立 OTA 上位机：BLE 扫描、SMP/MCUboot 固件上传、重启后校验和升级日志。
- 裸板测试上位机：SWD 烧录、串口测试判定和单文件记录。
- Windows 打包与离线安装脚本。

本仓库只保存源码、配置模板和构建脚本。固件、依赖安装器、程序安装包、真实工位配置、令牌以及生产记录不会提交。

## 运行环境

- 推荐系统：Windows 10/11 x64。
- Python：3.10 或更高版本。
- 半机/整机 UART：`pyserial`。
- Windows BLE/OTA：`bleak`、`smpclient`。
- GUI：`tkinter`、`ttkbootstrap`。
- 芯片烧录：Nordic `nrfjprog` 和兼容的 SEGGER J-Link DLL。
- nRF Dongle BLE：本机安装的 nRF Connect for Desktop Bluetooth Low Energy 应用。

Win7 配置仍保留，但当前功能和离线交付以 Win10 x64 为主要目标。Win7 不支持现代 Windows BLE 接口，仅适合 UART 场景，并需要单独准备兼容的 Python/PyInstaller 环境。

## 仓库结构

```text
AXI-WorkStation/
├─ tools/
│  ├─ factory_workstation/       # 半机/整机 GUI、CLI、MES、OTA、记录
│  ├─ ota_workstation/           # 独立 OTA GUI、配置、任务管理和自检
│  ├─ bare_board_workstation/    # 裸板 GUI、CLI、烧录与记录
│  ├─ ota_smp_dongle.py          # nRF Dongle OTA 辅助脚本
│  └─ requirements-workstation.txt
├─ packaging/                    # Windows 安装和离线包脚本
├─ Axi Factory Workstation.spec
├─ Axi Factory Workstation Win7.spec
├─ Axi Bare Board Workstation.spec
└─ 半机测试MES接口说明.md
```

## 从源码运行

在仓库根目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r tools\requirements-workstation.txt

Copy-Item tools\factory_workstation\config.json.example tools\factory_workstation\config.json
Copy-Item tools\factory_workstation\.env.example tools\factory_workstation\.env
python tools\factory_workstation\app.py
```

首次启动会要求设置工程师密码。工厂令牌可在工程模式中配置，也可以写入本机 `.env`：

```text
AXI_FACTORY_ENGINEER_TOKEN=...
AXI_FACTORY_RECOVER_TOKEN=...
AXI_FACTORY_ENGINEER_PASSWORD=...
```

`.env` 和 `config.json` 是本机文件，已由 `.gitignore` 排除。不要将真实凭据写入源码或配置模板。

裸板上位机：

```powershell
Copy-Item tools\bare_board_workstation\config.json.example tools\bare_board_workstation\config.json
python tools\bare_board_workstation\app.py
```

## 常用操作

GUI 启动后：

1. 在设置页选择 UART/BLE、端口、固件路径、J-Link ID 和 MES 工位信息。
2. 正式生产测试保持“启用 SN/记录”开启；临时联调可关闭该开关。
3. 扫码或输入 SN，连接设备，选择半机或整机测试。
4. 测试完成后分别查看设备结果和 MES 状态。
5. `MES：已确认上传` 表示 MES 返回满足业务成功规则；`MES：未确认` 表示请求已保存到待重传文件，不能视为上传成功。

正式记录默认保存到：

```text
factory_records/YYYY-MM-DD/<run_id>/unified_log.csv
```

MES 上传失败的请求保存到：

```text
factory_records/mes_pending/<run_id>.json
```

详细 GUI、CLI、BLE、烧录、OTA、MES 和记录说明见 [半机/整机使用说明](tools/factory_workstation/README.md)；裸板说明见 [裸板使用说明](tools/bare_board_workstation/README.md)。

独立 OTA 工具直接运行：

```powershell
python tools\ota_workstation\app.py
```

使用与打包说明见 [独立 OTA 上位机说明](tools/ota_workstation/README.md)。

## MES 对接状态

- 使用 HTTP POST 和 UTF-8 JSON。
- 正式半机/整机测试在最终设备结果产生后上传一次 `postxtdata`。
- 当前按响应字段 `res == "OK"` 判断 MES 业务成功，HTTP 2xx 本身不代表业务成功。
- `test_items` 包含逐项 PASS/NG、耗时、错误原因、响应摘要和解析后的采样数据。
- Touch/LRA/PPG 数据按通道数组上传，单测试项最多上传 1000 帧并显式标记截断。
- `checkroute` 默认关闭，待 MES 路由接口规则确认后再启用。
- `ECLIST.ERROR_CODE/LOCATION` 的正式映射仍需由生产/MES 提供。

接口字段示例见 [半机测试 MES 接口说明](半机测试MES接口说明.md)。当前代码已通过本地模拟回归，正式投产前仍须在 MES 内网验证真实落库、请求体限制和失败项展示。

## 测试

这些测试不连接真实硬件或 MES：

```powershell
python tools\factory_workstation\smoke_p1_0h.py
python tools\factory_workstation\smoke_mes.py
python -m tools.bare_board_workstation.smoke_bare_board
python -m tools.ota_workstation.smoke_ota
```

硬件、烧录、OTA 和 MES 内网测试必须在目标工位单独执行，不能用本地模拟结果代替。

## 打包与安装

完整步骤、输入文件、输出目录以及离线包安装方式见 [构建与安装说明](docs/BUILD_AND_INSTALL.md)。常用入口：

```powershell
# 半机/整机 Win10 程序目录
python -m PyInstaller --clean --noconfirm "Axi Factory Workstation.spec"

# 裸板 Win10 程序目录
python -m PyInstaller --clean --noconfirm "Axi Bare Board Workstation.spec"

# 在程序目录生成后制作半机/整机 Win10 离线 USB 包
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\build_offline_usb_win10.ps1 `
  -FirmwareHexPath "D:\firmware\merged.hex" `
  -FirmwareOtaPath "D:\firmware\zephyr.signed.bin" `
  -PackageRevision "r1"
```

所有输出都位于 `dist/`，该目录不会提交到 Git。

## 发布前检查

```powershell
git status --short
git diff --check
python tools\factory_workstation\smoke_p1_0h.py
python tools\factory_workstation\smoke_mes.py
python -m tools.bare_board_workstation.smoke_bare_board
```

确认以下文件没有进入暂存区：

- `tools/**/config.json`
- `tools/factory_workstation/.env`
- `build/`、`dist/`
- `factory_records/`、`bare_board_records/`
- 固件、EXE、ZIP、依赖安装器和现场日志
