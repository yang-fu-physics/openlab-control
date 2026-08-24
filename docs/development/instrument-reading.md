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

在清单中声明一次：

```toml
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

[readings.heater_output]
label = "Heater Output"
unit = "%FS"
decimals = 2
```

除 `main_reading` 外的条目自动显示为扫描器复选项。操作者勾选的键在
`config.auxiliary_readings` 中；`read_status()` 的 `auxiliary` 必须完整返回这些键。键和顺序
在运行中不能变化。

前面板主读数使用清单选择的模板；`readout` 以 2×2 最多放四个读数，第五个开始放到右侧
下一个 `readout`；`switch` 把 0/1 主读数显示为 Off/On，并显示清单指令按钮。控制面板和
开关面板的辅助读数也按相同规则排列。结构化字典保留数值类型、单位和
精度；不要把多个
值拼成一段字符串。

## 两类数据文件

| 文件 | 何时写 | 内容 |
| --- | --- | --- |
| 实验 DAT | 每个 `Measure` 结果行 | 与模块测量同步的主值和本次即时辅助值 |
| `instrument_status.dat` | Run 期间按独立周期 | 完整值、目标、速率、稳定性、连接和辅助读数 |

Data Browser 可打开任意 DAT，不绑定当前 Run。Live Trend 使用已有快照，不会额外读取仪表。

## `ready` 不能绕过核心判稳

`ready = false` 会阻止稳定；`ready = true` 以后，核心仍要检查目标误差、斜率和 dwell。没有
独立状态字时省略该键或返回 `None`。
