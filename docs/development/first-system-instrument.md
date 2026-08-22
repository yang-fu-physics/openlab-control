# 写第一个 System Instrument

这一页做一个最小只读温度仪表。先让“连接、读取、断开”跑通，再增加控制；这样更容易定位
协议问题，也不会在第一次测试时意外改变输出。

## 1. 复制骨架

把下面目录复制到安装目录：

```text
templates/system-instruments-repository/instruments/example_monitor/
```

复制后的结构例如：

```text
system_instruments/my_thermometer/
├─ instrument.toml
└─ backend.py
```

目录名和清单 ID 都使用小写字母、数字与下划线，并以字母开头。

## 2. 写清单

`instrument.toml` 告诉核心这份代码叫什么、从哪里加载、能提供什么类型的读数：

```toml
id = "my_thermometer"
name = "My Thermometer"
version = "0.1.0"
api_version = "1.2"
core_requires = ">=0.15,<0.16"
backend = "backend:MyThermometer"
kinds = ["monitor"]
dependencies = []

[discovery]
identity_pattern = "(?i)expected maker.*expected model"
primary_reading = "reading"
monitor_readings = []

[discovery.reading_labels]
reading = "Main Reading"
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定名称；配置中的 `backend` 使用它 |
| `version` | 代码有变化时递增 |
| `backend` | `Python 文件名:类名`，这里就是 `backend.py` 中的 `MyThermometer` |
| `kinds` | 允许用于 `temperature`、`field` 或 `monitor` 中的哪些类型 |
| `dependencies` | 额外第三方包；PyVISA 等框架已有依赖不用重复写 |
| `[discovery]` | 可选只读扫描建议；正则只匹配 `*IDN?`，不会导入后台或连接输出 |
| `[discovery.reading_labels]` | 把保存用内部键映射为扫描器下拉框和复选框中的英文名称；必须覆盖每个声明的主/辅助读数 |

## 3. 写后台类

下面示例展示接口位置。`open_transport` 和 `query_temperature` 代表你根据仪表手册写的有限
超时通讯代码；不要直接照抄成真实驱动。

```python
from __future__ import annotations

import time

from labcontrol.instruments.base import InstrumentError, SystemInstrument
from labcontrol.models import InstrumentActivity, InstrumentSnapshot


class MyThermometer(SystemInstrument):
    api_version = "1.2"

    def __init__(self, config, simulation_speed=1.0):
        super().__init__(config, simulation_speed)
        self._transport = None
        self._address = config.address

    async def connect(self):
        transport = None
        try:
            transport = open_transport(self._address, timeout_seconds=1.0)
            identity = transport.query("*IDN?")
            if "EXPECTED MODEL" not in identity.upper():
                raise InstrumentError(
                    f"Unexpected instrument: {identity}",
                    "IDENTITY_MISMATCH",
                    self._address,
                )
            # 先成功读一次，确认通讯和数值都正常；这里不改变仪表设置。
            query_temperature(transport)
            self._transport = transport
        except Exception:
            if transport is not None:
                transport.close()
            raise

    async def poll(self):
        if self._transport is None:
            raise InstrumentError("Instrument is not connected", "NOT_CONNECTED")
        value = float(query_temperature(self._transport))
        return InstrumentSnapshot(
            instrument_id=self.config.id,
            display_name=self.config.display_name,
            kind=self.config.kind,
            timestamp=time.monotonic(),
            connected=True,
            unit=self.config.unit,
            current=value,
            activity=InstrumentActivity.IDLE,
        )

    async def disconnect(self):
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
```

几个重要细节：

- `__init__` 只保存配置，不能连接或写仪表。
- `connect` 先用局部变量建立连接；中途失败也要关闭已经打开的句柄。
- `timestamp` 必须用 `time.monotonic()`，不能使用日期时间。
- `disconnect` 要允许调用多次，也要能清理只完成一半的初始化。
- 所有底层读取和写入都必须有有限超时。

## 4. 扫描并在现场配置中选择它

先运行：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

确认该地址用途为 `System`，选择 `my_thermometer`，保存资源 ID，例如
`sample_thermometer`。然后在现场主配置中引用它：

```toml
[[instruments]]
id = "sample_monitor"
display_name = "Sample Monitor"
kind = "monitor"
role = "monitor"
control_enabled = false
backend = "my_thermometer"
resource = "sample_thermometer"
unit = "K"
operation_timeout_seconds = 10.0
shutdown_timeout_seconds = 3.0
```

核心会把资源地址解析到 `config.address`。PID 表、协议超时或厂商专用选项仍可作为
`[[instruments]]` 的额外键，通过 `config.extras` 读取。

## 5. 第一次加载

1. 重启 OpenLab Control。
2. 核对首次信任窗口中的类型、ID、版本、路径和内容指纹。
3. 确认后，观察前面板是否出现读数。
4. 修改任何源文件后，内容指纹会变化，需要再次确认。

如果需要额外依赖，程序会提示从本地 wheel 安装；不会联网下载。PyVISA、PySide6、
QtAwesome、packaging 和 typing_extensions 使用框架锁定的统一版本。

## 6. 再增加控制

只有温度或磁场主控仪表才实现 `set_target()` 和 `hold()`。先在配置中声明真实上下限和最大
速率，再阅读[控制与安全](instrument-control-safety.md)。只读仪表不要写这两个方法；基类会
明确拒绝控制请求。
