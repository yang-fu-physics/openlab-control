# 写第一个 System Instrument

这一页做一个最小只读温度计。先完成连接、读取和断开，再考虑控制，第一次接真机时就不会
意外改变输出。

## 1. 复制骨架

复制：

```text
system_instruments/example_monitor/
```

得到：

```text
system_instruments/my_thermometer/
├─ instrument.toml
└─ backend.py
```

目录名和清单 `id` 使用小写字母、数字与下划线，并以字母开头。

## 2. 写 API v4 清单

```toml
id = "my_thermometer"
name = "My Thermometer"
version = "0.1.0"
api_version = "4"
core_requires = ">=0.19,<0.20"
backend = "backend:MyThermometer"
kinds = ["monitor"]

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

先理解这些字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 这份实现的稳定 ID，也必须与目录名一致 |
| `backend` | `Python 文件名:类名` |
| `kinds` | 支持 `temperature`、`field` 或 `monitor` |
| `[[panels]]` | 作者提供的固定面板；每项有稳定 `id` |
| `[readings.<id>]` | 每个读数的名称、单位和小数位 |
| `discovery.identity_pattern` | 用 `*IDN?` 文本辅助匹配 VISA 地址 |

`readout` 必须引用一个已声明读数；`readout_grid` 引用一到四个；`switch` 引用一个读数和
已声明的无参数指令。清单只接受 API v4 定义的字段，拼写错误会直接让模板变为 Invalid。

## 3. 增加型号专用输入

若通讯超时需要操作者确认，在清单增加：

```toml
[[config_fields]]
id = "visa_timeout_ms"
label = "VISA I/O Timeout (ms)"
type = "integer"
default = 1000
min = 1
```

可用类型是 `string`、`integer`、`number`、`boolean`、`choice` 和 `pid_file`。扫描器根据声明
生成表单，后台从 `config.extras["visa_timeout_ms"]` 读取最终值。不要在清单里加入只对某个
实验室有意义的真实地址或 PID 数字。

若仪表不是 VISA 设备，由对应模板声明自己的连接字段。例如专用网络仪表可以声明：

```toml
[[config_fields]]
id = "host"
label = "Host"
type = "string"
default = ""

[[config_fields]]
id = "port"
label = "Port"
type = "integer"
default = 502
min = 1
max = 65535
```

这类模板不需要 `discovery.identity_pattern`；扫描器会在它自己的步骤显示 Host/Port。

## 4. 写 `backend.py`

后端是普通同步 Python。下面的 `open_transport()` 和 `query_temperature()` 代表按仪表手册写的
通讯代码，不是通用驱动：

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

核心负责时间戳、名称、单位、连接状态和快照。后端只返回读数。所有底层 I/O 都必须设置有限
超时；打开到一半失败时仍要关闭句柄，`close()` 也要允许重复调用。

## 5. 用扫描器建立实例

运行：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

在 VISA 页确认地址，然后进入 **My Thermometer** 步骤：

1. 点击 **Add Instrument**；
2. 给物理实例一个全局唯一 ID，例如 `sample_thermometer`；
3. 选择检测到的 VISA 地址；
4. 确认型号专用字段；
5. 保持 `reading` 面板开启、角色为 `none`；
6. 在最后一页检查完整预览并保存。

扫描器生成的实例部分类似：

```toml
[[instances]]
id = "sample_thermometer"
resource = "USB0::...::INSTR"
identity = "Expected Maker,Expected Model,..."
visa_timeout_ms = 1000

[[instances.panels]]
id = "reading"
enabled = true
order = 1
role = "none"
```

生成文件还会复制清单中的静态元数据，但不复制 `panels` 模板。这个 VISA 地址归该 System
实例所有，因此不会再出现在 `configs/visa.resources.toml`。

## 6. 第一次加载

1. 先运行清单和假通讯测试。
2. 启动 OpenLab Control。
3. 观察面板读数与 Run Log；清单或实例错误会在连接真机前停止。
4. 连接安全负载，确认身份、单位、有限超时与重复关闭。

PyVISA、PySide6、QtAwesome、packaging 和 typing_extensions 使用框架锁定版本。新实现需要
其他 Python 包时，应更新核心锁定依赖、完成测试并重新构建发布包。

## 7. 再增加控制

温度控制器需要声明控制端点和 controller 面板：

```toml
[[controls]]
id = "main"
label = "Main Temperature"

[[panels]]
id = "control"
label = "Sample Temperature"
template = "controller"
control = "main"
reading_options = ["temperature"]
default_reading = "temperature"
min_value = 2.0
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 10.0
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.03
stability_dwell_seconds = 30.0
stability_timeout_seconds = 1800.0
stability_window_seconds = 30.0
```

再实现 `set_target(..., control=<id>)` 和 `hold(control=<id>)`。操作者在扫描器中确认限制，并
在确实作为主温度时把角色设为 `sample_temp`。详细安全要求见[控制与安全](instrument-control-safety.md)。

压缩机这类开关使用 `switch`，在清单声明 `[[sequence_commands]]`，并实现
`execute_sequence_command(command_id)`。同一指令 ID 同时用于面板按钮和右侧 System
Commands。
