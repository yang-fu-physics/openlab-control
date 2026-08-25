# 写第一个 System Instrument

这一页做一个最小只读温度仪表。先完成连接、读取和断开，再考虑控制。这样第一次接真机时
不会意外改变输出。

## 1. 复制骨架

复制：

```text
templates/system-instruments-repository/instruments/example_monitor/
```

得到：

```text
system_instruments/my_thermometer/
├─ instrument.toml
└─ backend.py
```

目录名和清单 `id` 使用小写字母、数字与下划线，并以字母开头。

## 2. 写 `instrument.toml`

```toml
id = "my_thermometer"
name = "My Thermometer"
version = "0.1.0"
api_version = "3"
core_requires = ">=0.18,<0.19"
backend = "backend:MyThermometer"
kinds = ["monitor"]
dependencies = []
main_reading = "temperature"

[panel]
template = "readout"

[discovery]
identity_pattern = "(?i)expected maker.*expected model"

[readings.temperature]
label = "Temperature"
unit = "K"
decimals = 3
```

只需理解这些字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 这份实现的固定名称 |
| `backend` | `Python 文件名:类名` |
| `kinds` | 可用作 `temperature`、`field` 或 `monitor` |
| `main_reading` | 前面板和标准 DAT 使用的主读数 |
| `[panel].template` | 底部面板；单个只读值用 `readout`，多个只读值用 `readout_grid`，温度/磁场控制用 `controller`，0/1 状态和简单按钮用 `switch` |
| `[readings.<键>]` | 每个读数的英文名称、单位和显示小数位 |
| `identity_pattern` | 扫描器用 `*IDN?` 返回值自动建议这份实现 |
| `dependencies` | 仅填写框架没有提供的额外包；PyVISA 不用重复写 |

`[panel]` 必须明确填写，不会猜测旧清单。`main_reading` 必须对应一个
`[readings.<键>]`。除主读数外，其余 `[readings]` 会自动成为扫描器
里的可选辅助读数，不需要再列第二遍。

清单只接受教程列出的字段。字段拼错或继续填写旧的辅助读数列表会直接显示为 Invalid，
不会被静默忽略。

## 3. 写 `backend.py`

后端是普通同步 Python。下面的 `open_transport()` 和 `query_temperature()` 代表按仪表手册写的
通讯代码，不是可以直接控制任意仪表的通用实现。

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
            timeout_seconds=1.0,
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
        return {
            "value": float(query_temperature(self._transport)),
            "auxiliary": {},
        }

    def close(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
```

核心负责时间戳、显示名称、单位、连接状态和内部快照。后端不要创建这些对象，只返回读数。
所有底层 I/O 都必须设置有限超时。

## 4. 扫描并选择地址

运行：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

把地址用途设为 `System`，选择 `my_thermometer`，保存资源名，例如
`sample_thermometer`。扫描器生成的本机文件类似：

```toml
[[resources]]
id = "sample_thermometer"
address = "USB0::...::INSTR"
identity = "Expected Maker,Expected Model,..."
purpose = "system"
system_instrument = "my_thermometer"
auxiliary_readings = []
```

主读数不在这里重复保存；它来自 `instrument.toml`。

现场主配置只引用资源：

```toml
[[instruments]]
id = "sample_monitor"
display_name = "Sample Monitor"
kind = "monitor"
resource = "sample_thermometer"
control_enabled = false
operation_timeout_seconds = 10.0
shutdown_timeout_seconds = 3.0
```

这里不再重复 `backend`、`unit`、`role` 或仿真用的 `initial_value`。协议超时、PID 表和厂商专用选项仍可写在该
`[[instruments]]` 中，并从 `config.extras` 读取。

## 5. 第一次加载

1. 重启 OpenLab Control。
2. 核对首次信任窗口中的类型、ID、版本、路径和内容指纹。
3. 确认后观察前面板读数。
4. 修改任何源文件后，内容指纹会变化，需要再次确认。

额外依赖只从本地 wheel 安装，不会联网下载。PyVISA、PySide6、QtAwesome、packaging 和
typing_extensions 使用框架锁定版本。

## 6. 再增加控制

温度或磁场控制器再把 `panel.template` 改为 `controller`，实现 `set_target()` 和 `hold()`，并在现场配置中写
`control_enabled = true`、真实上下限和最大速率。继续阅读[控制与安全](instrument-control-safety.md)。

需要压缩机这类简单开关时，使用 `switch`，在清单加入无参数 `[[sequence_commands]]`，并在
后端实现 `execute_sequence_command(command_id)`。清单中的指令会同时成为底部按钮和右侧
System Commands，不要再写第二套界面动作名称。
