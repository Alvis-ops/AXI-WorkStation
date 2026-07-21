# 半机测试MES接口说明

## 一、半机测试数据采集

### 1. 接口地址

- **测试前调用**：`http://192.168.3.58/json/J.php/xt/checkroute`
- **测试后调用**：`http://192.168.3.58/json/J.php/xt/postxtdata`

### 2. 请求方式

HTTP 协议 POST 请求；内容为 JSON 格式数据字符串，字符集 UTF-8

---

### >>> checkroute

**接口地址**：`http://192.168.3.58/json/J.php/xt/checkroute`

#### 数据范例

```json
{
    "Device ": "DE0001",
    "Line": "L1",
    "Station": "半机测试",
    "SN": "JP00001"
}
```

| 字段 | 说明 |
|------|------|
| Device | 设备编号 |
| Line | 线体名称 |
| Station | 工位名称 |
| SN | 产品条码 |

---

### >>> postxtdata

**接口地址**：`http://192.168.3.58/json/J.php/xt/postxtdata`

#### 数据范例

```json
{
    "Device": "DE0001",
    "Line": "L1",
    "Station": "半机测试",
    "SN": "JP00001",
    "ProcessStartTime": "202105271804",
    "ProcessEndTime": "202105271805",
    "schema_version": "1.0",
    "run_id": "20260717_105603_HALF_123456",
    "station_type": "HALF",
    "dut_alias": "",
    "device_result": "PASS",
    "device_message": "completed",
    "test_items": {
        "ppg_dark_capture": {
            "name": "PPG dark capture",
            "result": "PASS",
            "elapsed_ms": 1008,
            "error_reason": "",
            "response_summary": "+HW:PPG:CAPTURE:samples=1,status=PASS,dark0=7,dark1=8 ; OK",
            "measurements": {
                "summary": [
                    {
                        "kind": "ppg_capture",
                        "category": "ppg",
                        "fields": {
                            "samples": "1",
                            "status": "PASS",
                            "dark0": "7",
                            "dark1": "8"
                        },
                        "line": "+HW:PPG:CAPTURE:samples=1,status=PASS,dark0=7,dark1=8"
                    }
                ],
                "samples": {
                    "seq": ["0"],
                    "ms": ["50"],
                    "green0": ["1"],
                    "red0": ["2"],
                    "ir0": ["3"],
                    "green1": ["4"],
                    "red1": ["5"],
                    "ir1": ["6"],
                    "dark0": ["7"],
                    "dark1": ["8"],
                    "mask": ["0x03"]
                },
                "sample_count": 1,
                "uploaded_sample_count": 1,
                "truncated": false
            }
        }
    },
    "failed_items": {},
    "ECLIST": [
        {
            "ERROR_CODE": "M107",
            "LOCATION": "U10"
        }
    ]
}
```

| 字段 | 说明 |
|------|------|
| Device | 设备编号 |
| Line | 线体名称 |
| Station | 工位名称 |
| SN | 产品条码 |
| ProcessStartTime | 测试开始时间 |
| ProcessEndTime | 测试结束时间 |
| schema_version | 上位机上传数据结构版本 |
| run_id | 单次测试运行标识 |
| station_type | 测试类型：`HALF` / `FULL` |
| dut_alias | 上位机中的设备别名；没有时为空字符串 |
| device_result | 设备测试结果：`PASS` / `FAIL` |
| device_message | 设备测试最终消息 |
| test_items | 全部测试项；每项可包含结果、耗时、日志摘要及嵌套采集数据 |
| failed_items | 失败项对象，以稳定测试项键名索引；无失败时传空对象 `{}` |
| ECLIST | 没有不良，则传空数组；有不良时，传不良代码和不良位置 |

每个测试项的 `measurements` 结构固定为：

- `summary`：解析后的采集汇总、探测值和状态数组。
- `samples`：按通道存储的采样数组，例如 `green0: ["123", "122", ...]`。所有数组以相同下标关联同一帧，字段缺失时对应位置为 `null`；不重复上传原始 AT 文本。
- `sample_count`：上位机实际收到的采样帧总数。
- `uploaded_sample_count`：本次请求实际携带的采样帧数。
- `truncated`：是否因单项超过 1000 帧而截断。正常生产采样配置低于该上限。

`schema_version`、`run_id`、`station_type`、`dut_alias`、`device_result`、
`device_message`、`test_items` 和 `failed_items` 均位于请求 JSON 最外层，
不再使用旧版 `Result` 字段或 `Data` 包装对象。
