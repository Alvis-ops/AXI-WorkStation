# AXI OTA 上位机

这是从半机/整机上位机中独立出来的 BLE SMP/MCUboot OTA 工具，不包含 SN、MES、工厂测试、UART 和芯片烧录功能。

## 独立使用

打包后的程序不需要安装 Python，也不依赖原半机/整机上位机。请完整保留并复制 `dist/Axi OTA Workstation/` 目录，不能只复制主 EXE；主程序、两个 OTA Helper 和 `_internal` 运行库需要放在一起。

运行时仍需准备：

- 签名 `.bin` 或 DFU `.zip` OTA 固件。
- Windows 蓝牙模式所需的系统 BLE 适配器；或 nRF Dongle 模式所需的 Nordic Dongle。
- nRF Dongle 模式需要安装 nRF Connect for Desktop Bluetooth Low Energy app。
- 目标设备固件需要启用 MCUboot SMP BLE 服务。

## 运行

```powershell
python tools\ota_workstation\app.py
```

操作顺序：

1. 选择 `nRF Dongle` 或 `Windows 蓝牙`。
2. 选择签名 `.bin` 或 DFU `.zip` 固件。
3. 扫描设备，双击目标设备；也可以直接填写 BLE 地址。
4. 点击“开始 OTA”，确认设备、固件和后端。
5. 等待上传、重启和镜像校验完成。

默认使用 `safe` 速度。链路稳定后可选择 `balanced`。需要 GATT 认证的固件应选择 Windows 蓝牙并打开“Windows 认证/配对”。nRF Dongle 模式需要正确选择 Dongle CDC COM 口。

程序将本机设置保存到 `tools/ota_workstation/config.json`；打包后保存到 EXE 同目录的 `config.json`。该文件是机器相关配置，不应提交。

## 无硬件自检

```powershell
python -m tools.ota_workstation.smoke_ota
```

## 打包

工作区中需要同时存在相邻固件仓库 `..\axi-p1-embeded`，然后执行：

```powershell
python -m PyInstaller --clean --noconfirm "Axi OTA Workstation.spec"
```

输出目录 `dist/Axi OTA Workstation/` 包含：

- `Axi OTA Workstation.exe`
- `Axi OTA BLE Helper.exe`
- `Axi OTA Dongle Helper.exe`
- nRF Dongle 扫描和 NUS/SMP 所需的 JavaScript 文件

真实 OTA 必须在目标 Windows 工位和设备上验证；无硬件自检不会模拟 OTA 成功。
