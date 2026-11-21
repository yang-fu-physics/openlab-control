# 写第一个 System Instrument

这一节带你做一个最小的只读温度计。开发真实系统仪表时，**强烈建议先完成只读连接与监控，验证无误后再加控制写入**，这样首次上机绝不会意外改写设备输出。

## 1. 复制只读骨架

把自带的 `system_instruments/example_monitor/` 复制为新目录（目录名须与清单 `id` 一致，小写字母开头，只含小写字母、数字和下划线）：

```text
system_instruments/my_thermometer/
├─ instrument.toml
└─ backend.py
```

## 2. 编写 `instrument.toml`

```toml
id = "my_thermometer"
name = "My Thermometer"
version = "0.1.0"
api_version = "4"
core_requires = ">=0.19,<0.21"
backend = "backend:MyThermometer"
kinds = ["monitor"]

[[config_fields]]
id = "visa_timeout_ms"
label = "VISA I/O Timeout (ms)"
type = "integer"
default = 1000
min = 1

[[panels]]
id = "reading"
label = "Temperature"
template = "readout"
readings = ["temperature"]

[discovery]
identity_pattern = "(?i)expected maker.*expected model"

[readings.temperature]
label = "Temperature"
unit = "K"
decimals = 3
```

关键字段速览：

| 字段 | 作用 |
| --- | --- |
| `id` / `backend` | 稳定标识（与目录名一致）和入口 `文件名:类名` |
| `kinds` | 支持 `temperature`、`field`、`monitor` |
| `[[config_fields]]` | 型号专用输入（`string` / `integer` / `number` / `boolean` / `choice` / `ordered_choices` / `pid_file`），后台通过 `config.extras[...]` 读取 |
| `[[panels]]` | 固定面板定义；`controller` 面板还可设 `setpoint_decimals`、`rate_decimals` 和低温加密阈值 `fine_setpoint_below` |
| `[readings.<id>]` | 读数显示名、单位和小数位 |
| `[discovery]` | 填 `identity_pattern` 时扫描器自动提供 VISA 下拉选择；网口仪表省略此块，改在 `config_fields` 声明 `host` 和 `port` |

> 清单字段名区分大小写且严格校验，拼写错误会让模板在扫描器中显示为 Invalid。

## 3. 编写 `backend.py`

下面的 `open_transport()` 和 `query_temperature()` 代表你按仪表手册写的底层通信函数（建议放在独立的 `instrument.py` 里）：

```python
from pyvisa.errors import VisaIOError

from labcontrol.instruments.base import InstrumentError, SystemInstrument


class MyThermometer(SystemInstrument):
    def __init__(self, config):
        super().__init__(config)
        self._transport = None

    def open(self):
        transport = open_transport(
            self.config.address,
            timeout_ms=self.config.extras["visa_timeout_ms"],
        )
        try:
            identity = transport.query("*IDN?")
            if "EXPECTED MODEL" not in identity.upper():
                raise InstrumentError(
                    f"Unexpected instrument: {identity}",
                    "IDENTITY_MISMATCH",
                    self.config.address,
                )
            value = float(query_temperature(transport))
            if not 2.0 <= value <= 400.0:
                raise InstrumentError(
                    f"Temperature is outside the validated range: {value}",
                    "TEMPERATURE_OUT_OF_RANGE",
                    self.config.address,
                )
        except (VisaIOError, OSError, ValueError, InstrumentError):
            transport.close()
            raise
        self._transport = transport

    def read_status(self):
        if self._transport is None:
            raise InstrumentError("Instrument is not open", "NOT_OPEN")
        return {"value": float(query_temperature(self._transport))}

    def close(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
```

写 `backend.py` 记住三条：

1. `__init__` 只保存配置，不要连接硬件；
2. 每次底层 I/O 都要有有限超时，`open()` 中途报错时必须把已打开的句柄关掉；
3. `close()` 必须可以重复调用而不报错。

## 4. 用扫描器生成现场实例

运行扫描器（源码版 `.\.venv\Scripts\python.exe .\tools\instrument_scanner.py`），在 **VISA Resources** 扫描后进入 **My Thermometer** 页：

1. 点击 **Add Instrument**，填写唯一实例 ID（如 `sample_thermometer`）；
2. 选择检测到的 VISA 地址，核对 `visa_timeout_ms`；
3. 保持 `reading` 面板开启、角色为 `none`；
4. 在最后一页检查完整写入预览并保存。

扫描器会自动把该实例写入 `configs/instruments/my_thermometer.toml`，并将该 VISA 地址从测量模块资源表中移除。随后先跑假连接单元测试，再启动 OpenLab Control 观察面板读数和 Run Log。

## 5. 需要控制功能时怎么扩展？

只读跑稳后，若要升级为温度/磁场控制器，只需两步：

1. 在 `instrument.toml` 中声明 `[[controls]]` 端点，并添加一个 `template = "controller"` 的面板（附带上下限、默认/最大速率与判稳参数默认值）；
2. 在 `backend.py` 中实现 `set_target(value, rate_per_minute, mode="Settle", *, control)` 和 `hold(*, control)`。

如果是压缩机这类开关设备，则使用 `template = "switch"` 面板，在清单声明 `[[sequence_commands]]` 并在后台实现 `execute_sequence_command(command_id)`。详细安全要求见 [控制与安全](instrument-control-safety.md)。
