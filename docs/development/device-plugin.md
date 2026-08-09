# 更换温度或磁场设备

!!! info "本页不是 Measurement Module 教程"

    如果你在开发电阻、电压或电流测量，请跳过本页。Device Plugin 只负责主温度、主磁场
    和只读监视设备。

OpenLab Control 不控制 PPMS。这里的插件用于连接程序自己管理的温控仪、磁场控制器或只读
监视仪表。

## 更换设备时需要改什么

每种设备准备一个插件。设备确定后，只需在主配置中写入插件名称、通讯地址和允许范围，然后
重启程序。通常不需要为不同仪表维护不同的主程序分支。

```text
device_plugins/my_controller/
├─ device.toml
└─ backend.py
```

`device.toml` 写名称、版本和设备种类：

```toml
id = "my_controller"
name = "My Temperature Controller"
version = "0.1.0"
api_version = "1.0"
core_requires = ">=0.13,<0.14"
backend = "backend:MyController"
kinds = ["temperature"]
dependencies = []
```

种类可以是：

- `temperature`：主温度或温度监视；
- `field`：主磁场或磁场监视；
- `monitor`：只读显示，例如 2nd Stage。

## 后台代码需要做的事情

```python
from labcontrol.devices.base import DevicePlugin


class MyController(DevicePlugin):
    async def connect(self): ...
    async def poll(self): ...
    async def set_target(self, value, rate_per_minute, mode="Settle"): ...
    async def hold(self): ...
    async def disconnect(self): ...
```

| 方法 | 用途 |
| --- | --- |
| `connect` | 连接仪表、确认型号、读取当前状态 |
| `poll` | 定期读取当前值、目标、速率和是否稳定 |
| `set_target` | 设置新的目标和速率 |
| `hold` | 保持刚刚读到的当前状态 |
| `disconnect` | 关闭连接 |

只读设备只需要连接、读取和断开；它不能接受目标值。

## 连接时不要自动改变仪表

`connect` 应按顺序完成：

1. 用有限等待时间打开连接；
2. 读取仪表型号；
3. 确认型号正确；
4. 读取当前值、目标和状态；
5. 不自动发送保存设置，不改变目标或输出。

插件文件被发现或读取时，也不能提前连接仪表。

## 在主配置中选择插件

```toml
[[devices]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
role = "primary"
plugin = "my_controller"
unit = "K"
min_value = 1.8
max_value = 400.0
max_rate_per_minute = 20.0
address = "GPIB0::12::INSTR"
```

`primary` 表示主设备。同一种类最多只有一个主设备；其他同类设备默认只监视。更换仪表时，
通常只需要更换 `plugin`、`address` 和允许范围，然后重启。

## 安全检查不能只做一次

目标值和速率至少要经过：

1. 主程序检查手动输入和 SEQ 参数；
2. 插件在发送命令前再次检查；
3. 仪表本机限制和硬件联锁作最后保护。

停止 SEQ 后，温度和磁场保持当前状态，不自动回零。需要 Hold 时，必须先读取当前真实值，
不能使用很久以前保存的数值。写命令等待超时后，也不要直接重发危险命令。

可以复制的例子位于：

- `plugin_templates/device-plugins-repository/plugins/example_controller/`
- `plugin_templates/device-plugins-repository/plugins/example_monitor/`

这些只是程序结构示例，不代表已经适合任何真实仪表。连接前请完成
[安全清单](../guides/safety-checklist.md)。
