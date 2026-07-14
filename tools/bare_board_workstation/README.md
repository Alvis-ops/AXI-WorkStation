# Bare Board Workstation

独立裸板测试上位机，用于后续迁移裸板测试固件。它与现有 `factory_workstation` 并列维护，默认流程是：

1. 操作员输入 SN。
2. 上位机通过 SWD 烧录配置的裸板测试固件。
3. 烧录完成后打开串口采集测试固件输出。
4. 将 SN、烧录信息和串口测试日志写入同一个 `.log` 文件。

## 目录

```text
tools/bare_board_workstation/
  app.py                 # GUI 入口
  cli.py                 # CLI 入口
  config.py              # 配置和 SN 规则
  flash_runner.py        # SWD 烧录封装
  serial_runner.py       # 串口日志采集
  records.py             # 单文件记录
  flow.py                # 烧录 -> 串口采集流程
  smoke_bare_board.py    # 无硬件 smoke
  config.json.example    # 配置模板
```

## 配置

复制模板后填写本机参数：

```powershell
Copy-Item tools\bare_board_workstation\config.json.example tools\bare_board_workstation\config.json
```

关键字段：

- `flash_image_path`：裸板测试固件镜像，默认用于 `nrfjprog --program`。
- `jlink_dll_path`：固定传给 `nrfjprog --jdll` 的 JLinkARM DLL；离线安装包会自动写入兼容版本。
- `nrfjprog_family`：传给 `nrfjprog --family`。POC3A（nRF54L15）建议留空，由 nrfjprog 自动识别（`AUTO`）；也可显式填 `NRF54L`。不要用 Zephyr 芯片名 `NRF54L15_XXAA`。
- `jlink_probe_id`：多 Probe 环境下的 J-Link 序列号。
- `serial_port` / `serial_baudrate`：测试固件日志串口。
- `test_start_command`：打开串口后需要主动发送的启动命令；为空时只监听固件输出。
- `pass_patterns` / `fail_patterns` / `end_patterns`：串口行日志判定规则。
- `records_root`：记录文件目录。

## 运行

GUI（ttkbootstrap 风格，布局参考 `factory_workstation`）：

```powershell
pip install -r tools\requirements-workstation.txt
Copy-Item tools\bare_board_workstation\config.json.example tools\bare_board_workstation\config.json
python tools\bare_board_workstation\app.py
```

CLI：

```powershell
python tools\bare_board_workstation\cli.py --config tools\bare_board_workstation\config.json.example --sn SN001 --port COM18 --jlink-probe-id 69730371
```

无硬件模拟：

```powershell
python -m tools.bare_board_workstation.smoke_bare_board
python tools\bare_board_workstation\cli.py --dry-run --sn SN001 --records-root .\bare_board_records
```

## 记录文件

记录文件默认位于：

```text
bare_board_records/YYYY-MM-DD/YYYYMMDD_HHMMSS_BARE_<SN>.log
```

文件内包含 SN、烧录配置、Probe、串口参数、`[FLASH]` 行、`[SERIAL_RX]` 行和最终结果。

## 验证边界

当前主机侧框架支持无硬件 smoke；真实 SWD 烧录和串口测试需要在裸板测试固件迁移后，用实机执行 PASS-HW 验证。
