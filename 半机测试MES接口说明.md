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
    "Result": "PASS",
    "Data": {
        "测试项1": "值1",
        "测试项2": "值2"
    },
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
| Result | 测试结果 PASS/FAIL |
| Data | 测试项及测试内容，JSON 格式 |
| ECLIST | 没有不良，则传空数组；有不良时，传不良代码和不良位置 |
