# System Instrument 从这里开始

System Instrument 负责实验期间一直存在的仪表，例如主温控仪、主磁场控制器、压缩机开关，
以及一直显示的冷头温度、压力或液位监视仪表。

电阻表、电压表、换向器和只在 `Measure` 时工作的测量组合属于 Measurement Module。

## 先判断写哪一种

| 需求 | 类型 |
| --- | --- |
| `Set Temperature` 或 `Scan Temperature` 控制它 | System Instrument |
| `Set Field` 或 `Scan Field` 控制它 | System Instrument |
| 程序一直显示，但从不设置 | System Instrument（`monitor`） |
| 程序一直显示，并提供简单的 On/Off 系统指令 | System Instrument（`monitor` + `switch`） |
| 只在 `Measure` 时读取 | Measurement Module |
| 有测量通道、设置窗口或模块 SEQ 指令 | Measurement Module |

System Instrument 通常随实验系统固定；Measurement Module 会按实验频繁 Enable 或 Disable。

## 运行流程

```text
configs/site.local.toml
        │ [[resources]]：地址 + System Instrument ID + 选中的辅助读数
        │ [[instruments]]：resource + 控制许可 + 安全范围
        ▼
system_instruments/<id>/instrument.toml
        │ backend + 面板模板 + 主读数 + 所有读数的名称/单位/精度
        ▼
独立子进程 ── 串行访问 ── VISA / TCP / 串口 / 厂商库 ── 真实仪表
```

后端只有几个容易理解的方法：

- `open()`：连接并确认身份和初始状态；
- `read_status()`：完整状态，供前面板、安全检查、判稳和状态日志使用；
- `read_measurement()`：可选的快速主读数；不写时自动调用 `read_status()`；
- `set_target()`：可控温度或磁场仪表设置目标；
- `hold()`：用新鲜读数保持当前状态；
- `execute_sequence_command()`：可选，执行清单声明的简单无参数指令；
- `event_responses()`：可选，为该仪表产生的稳定事件代码注册核心响应；
- `close()`：释放通讯，不擅自改变输出。

这些方法都是普通同步函数。核心负责子进程、超时、调用顺序、时间戳和内部状态对象。

## 一个目录对应一种物理型号/协议

```text
system_instruments/my_temperature_controller/
├─ instrument.toml
├─ backend.py
├─ instrument.py          # 可选：底层命令与响应解析
├─ requirements.lock      # 仅有框架外依赖时需要
└─ wheels/                # 可选：额外依赖的离线 wheel
```

不要按 TempA、TempB 或“主/副温度”拆目录。同一地址只创建一个实例和一个通讯会话；一台仪表
返回的其他值放在 `auxiliary` 字典中。不同物理地址各有独立子进程，可以并发轮询。

## 两份文件各管一件事

`instrument.toml` 由作者提供，描述实现和读数：

```toml
id = "cryocon_22c_24c"
backend = "backend:Cryocon22C24CController"
kinds = ["temperature"]
main_reading = "temp_b"

[panel]
template = "controller"

[readings.temp_b]
label = "Sample Temperature (Temp B)"
unit = "K"
decimals = 3

[readings.temp_a]
label = "Cold Head Temperature (Temp A)"
unit = "K"
decimals = 3
```

`configs/site.local.toml` 由操作者保存在本机。扫描器只更新其中的资源区块：

```toml
[[resources]]
id = "cryocon_main"
address = "USB0::...::INSTR"
purpose = "system"
system_instrument = "cryocon_22c_24c"
auxiliary_readings = ["temp_a"]

[[instruments]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
resource = "cryocon_main"
control_enabled = true
min_value = 2.0
max_value = 400.0
max_rate_per_minute = 10.0
```

同一个信息只写一次：地址和实现选择在同一现场配置的资源区块；面板模板、主读数、单位和
精度在清单；控制许可和安全范围在同一现场配置的 `[[instruments]]`。扫描器不会改写安全
范围。外部 System Instrument 的 `[[instruments]]` 不再接受重复的 `backend`、
`unit`、旧 `role` 或仿真的 `initial_value`。清单中的未知字段也会直接标记为 Invalid。

## `kind` 与控制许可

- `temperature`：进入标准温度列，可控也可只读；
- `field`：进入标准磁场列，可控也可只读；
- `monitor`：只显示和记录，不能控制。

每种 temperature/field 最多一个 `control_enabled = true` 的实例。SEQ 自动选择它。没有启用
控制的 temperature/field 仍可显示和记录；monitor 永远不能启用控制。

`instrument.toml` 必须在 `[panel]` 中选择一个模板：`controller` 显示当前值、目标、速率和
稳定状态，并在获准控制时允许双击；`readout` 只显示一个主读数；`readout_grid` 以 2×2
最多显示四个读数；`switch` 把主读数 0/1 显示为 Off/On，并把该清单的全部
`sequence_commands` 显示为按钮。按钮和 SEQ 使用同一指令 ID，不需要再写第二份面板 action
列表。`controller` 和 `switch` 的辅助读数也进入网格，第五个读数开始使用右侧的下一个
网格。可控温度/磁场实例必须使用 `controller`。

简单系统指令在清单中这样声明：

```toml
[panel]
template = "switch"

[[sequence_commands]]
id = "compressor_on"
label = "Compressor On"

[[sequence_commands]]
id = "compressor_off"
label = "Compressor Off"
```

只有现场配置实际选择这份 System Instrument 时，`Compressor On` 和 `Compressor Off` 才会
直接出现在右侧 **System Commands**。它们没有参数、不会生成 DAT 测量行；Warning 继续 SEQ，
Error 中止。`switch` 面板的按钮只在 SEQ 空闲时可用，核心仍会在运行时阻止抢占 SEQ 控制权。

设备相关的事件响应也写在 System Instrument 代码中，而不是写进核心。最小声明如下：

```python
from labcontrol.instruments.base import EventResponseSpec


def event_responses(self):
    return (
        EventResponseSpec(
            code="COLD_HEAD_ALARM",
            context="A",
            action="zero",
        ),
    )
```

`source` 自动使用当前逻辑仪表 ID。省略 `target_instrument` 时选择唯一可控磁场仪表；明确
填写时必须是一个可控 `field` ID。`zero` 使用目标磁场配置中的
`default_rate_per_minute`，仍经过核心上下限、超时和串行队列。后端不能取得另一台仪表
对象，也不能传入 Python 回调。当前正式 System Instrument 暂未注册任何响应，所以默认
运行行为不变。完整安全语义见[控制与安全](instrument-control-safety.md)。

## 阅读顺序

1. [写第一个 System Instrument](first-system-instrument.md)
2. [读取、前面板与日志](instrument-reading.md)
3. [控制与安全](instrument-control-safety.md)
4. [测试与现场接入](instrument-testing.md)

!!! warning "示例不是通用真实驱动"

    核心模板故意在连接真实仪表前报错。完成型号确认、有限 I/O 超时、现场上下限、写后
    回读、Hold、断线恢复和硬件联锁测试后，才能用于真实实验。
