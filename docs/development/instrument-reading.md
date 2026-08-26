# 读取、前面板与日志

多数 System Instrument 只需写 `read_status()`。只有完整状态读取明显较慢时，才额外写
`read_measurement()`。

## `read_status()`：完整状态

核心周期性调用它，用于前面板、温度/磁场判稳、Live Trend、报警和
`instrument_status.dat`。返回一个普通字典：

```python
return {
    "value": temp_b,
    "target": setpoint,
    "rate": ramp_rate,
    "moving": ramping,
    "ready": not ramping,
    "auxiliary": {
        "temp_a": temp_a,
        "heater_output": heater_percent,
        "heater_range": range_name,
    },
}
```

字段很少：

| 键 | 必需 | 含义 |
| --- | --- | --- |
| `value` | 是 | 主读数；数值或 `None` |
| `target` | 否 | 当前目标 |
| `rate` | 否 | 每分钟速率 |
| `moving` | 否 | 仪表是否正在改变值 |
| `ready` | 否 | 仪表自己的就绪/稳定状态字 |
| `auxiliary` | 否 | 选中的附加读数 |

上表是单控制端点的简写。若同一实例启用了多个不同的 control，顶层仍返回 `value` 和
`auxiliary`，但每个回路的 `target/rate/moving/ready` 必须放进
`controls[control_id]`。完整示例见 [System Instrument 从这里开始](system-instrument.md#multiple-controls)。

核心从 `instrument.toml` 取得名称、单位和小数位，并负责时间戳、连接状态和内部快照。后端
不要重复返回这些元数据。数值必须有限；附加值可以是有限数值、短文字、布尔值或 `None`。

完整状态读取仍要检查所有安全状态，即使操作者没有勾选某个辅助读数。例如 TempA 未显示时，
TempA 过温仍必须报错。

## `read_measurement()`：写 DAT 前的即时主值

Measurement Module 调用 `api.instruments()` 时，核心请求一次即时仪表读数，不复用最多一秒前
的前面板缓存。同一时刻多个模块的请求会合并。

基类默认调用 `read_status()`。若完整查询需要很多命令，可只实现：

```python
def read_measurement(self):
    return {"value": read_sample_temperature(self._transport)}
```

本次没有同步读取的辅助列会写空，不会沿用旧值。后台 `read_status()` 仍持续负责完整安全
检查；`read_measurement()` 自己读到无效主值时也必须报错。

## 同一连接不会被并发访问

核心对每个 System Instrument 串行执行：

1. 已经开始的一条仪表指令先完成；
2. 等待中的控制和安全操作优先；
3. `read_measurement()` 先于尚未开始的普通 `read_status()`；
4. 后台 `read_status()` 最后执行。

“优先”不会中断已经发出的命令，所以底层 I/O 仍必须有有限超时。后端不要另开线程访问同一
VISA Session。

## 一台仪表返回多个值

在 API v4 清单中分别声明固定面板和读数：

```toml
[[controls]]
id = "loop_1"
label = "Loop 1"

[[panels]]
id = "control"
label = "Sample Temperature"
template = "controller"
control = "loop_1"
reading_options = ["temp_b"]
default_reading = "temp_b"
min_value = 2.0
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 10.0
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.05
stability_dwell_seconds = 30.0
stability_timeout_seconds = 1800.0
stability_window_seconds = 30.0

[[panels]]
id = "monitor"
label = "Temperature and Heater"
template = "readout_grid"
readings = ["temp_a", "heater_output"]

[readings.temp_b]
label = "Sample Temperature (Temp B)"
unit = "K"
decimals = 3

[readings.temp_a]
label = "Cold Head Temperature (Temp A)"
unit = "K"
decimals = 3

[readings.heater_output]
label = "Heater Output"
unit = "%FS"
decimals = 2
```

扫描器为实例列出这两个固定面板。操作者可以分别开启、关闭和排序它们，并给 controller
选择 `temp_b` 与角色；不能临时创建作者未声明的面板。运行时会收集所有已开启面板引用的
读数。`read_status()` 的 `auxiliary` 应返回这些附加键，键集合在运行中不能变化。

`readout` 显示一个读数，`readout_grid` 显示一到四个，`switch` 把 0/1 状态显示为 Off/On
并提供清单引用的指令按钮。多个面板仍共用同一物理实例和通讯会话。结构化字典保留数值
类型、单位和精度；不要把多个值拼成一段字符串。

一个实例有多个 Controller 时，各面板分别保存目标、速率、动作、ready 和核心判定的
稳定状态。标准温度或磁场角色对应的回路仍作为实验 DAT 的主控制值；所有已启用 Controller
的完整独立状态都写入 `instrument_status.dat`。

## 两类数据文件

| 文件 | 何时写 | 内容 |
| --- | --- | --- |
| 实验 DAT | 每个 `Measure` 结果行 | 与模块测量同步的主值和本次即时辅助值 |
| `instrument_status.dat` | Run 期间按独立周期 | 每个 Controller 的独立状态，以及每台物理仪表一次连接信息和辅助读数 |

Data Browser 可打开任意 DAT，不绑定当前 Run。Live Trend 使用已有快照，不会额外读取仪表。

## `ready` 不能绕过核心判稳

`ready = false` 会阻止稳定；`ready = true` 以后，核心仍要检查目标误差、斜率和 dwell。没有
独立状态字时省略该键或返回 `None`。
