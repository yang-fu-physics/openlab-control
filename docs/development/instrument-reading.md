# 读取、前面板与日志

多数系统仪表只需要实现一个 `read_status()`。只有当完整状态查询很慢（例如要发十几条命令）、会影响实验采样速度时，才需要额外覆盖 `read_measurement()`。

## 1. `read_status()`：完整状态轮询

核心按周期调用它来刷新底部面板、判定温场稳定、绘制 Live Trend、触发报警以及写入 `instrument_status.dat`。返回值是一个普通 Python 字典：

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

| 键 | 必需 | 说明 |
| --- | --- | --- |
| `value` | 是 | 主读数（有限数值或 `None`） |
| `target` | 否 | 当前目标值（单回路简写） |
| `rate` | 否 | 当前每分钟变化速率 |
| `moving` | 否 | 仪表是否正在升降温/升降场（bool） |
| `ready` | 否 | 仪表自身的状态字是否就绪（bool，没有就省略或填 `None`） |
| `auxiliary` | 否 | 清单中声明的附加读数字典（数值、短文本、布尔值或 `None`） |

> **多回路控制器**：若同一物理实例启用了多个不同的 `control`，顶层仍返回 `value` 和 `auxiliary`，但每个回路各自的 `target / rate / moving / ready` 必须放进 `controls[control_id]` 返回，详见 [多个独立控制回路](system-instrument.md#multiple-controls)。

名称、单位和小数位统一来自 `instrument.toml`，后端不需要重复返回。即使操作员在界面上隐藏了某个辅助读数，`read_status()` 也仍应照常检查该仪表的关键安全状态。

## 2. `read_measurement()`：测量时刻的快速读取

当测量模块调用 `api.instruments()` 或核心准备写一条实验 DAT 行时，框架会主动请求一次**测量专用即时读取**，而不直接拿前面板最多 1 秒前的旧缓存敷衍。同一时刻多个模块的请求会自动合并为一次查询。

默认基类直接调用 `read_status()`。如果完整状态太慢，可以只覆盖 `read_measurement()` 读最关键的主值：

```python
def read_measurement(self):
    return {"value": read_sample_temperature(self._transport)}
```

本次没有同步查的附加列会干净地留空，不会混入旧缓存值；后台常规 `read_status()` 依然按周期运行并负责完整安全检查。

## 3. 核心如何保证同一连接绝不并发冲突？

每个物理仪表实例的所有操作都走同一个**带优先级的串行队列**，绝不会同时向同一串口/GPIB 发两条命令：

1. 已经发出的当前指令先跑完（不会被中途强行打断，因此底层必须设有限超时）；
2. 等待中的**安全联动响应**与**控制写命令**最先执行；
3. 等待中的 `read_measurement()` 排在尚未开始的普通轮询之前；
4. 常规后台 `read_status()` 最后执行。

后端请保持纯同步，不要自己再另开后台线程去抢同一个 VISA 会话。

## 4. 一台仪表展示多个面板

以温控仪为例，可以在同一份 `instrument.toml` 中声明一个 `controller` 面板加一个 `readout_grid` 面板，共用同一物理连接：

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

请返回结构化的数字字典，不要把多个值拼成一长段字符串。

## 5. 两份日志各自记什么？

| 文件 | 写入时机 | 记录内容 |
| --- | --- | --- |
| 实验 DAT（如 `experiment.dat`） | 每条 `Measure` 结果行 | 测量时刻的同步主值与模块测量列 |
| `instrument_status.dat` | Run 期间按独立周期（默认 1 秒） | 每个控制回路的当前值、目标、速率、动作、稳定性，以及每台物理仪表的连接状态与全部附加读数 |

## 6. `ready` 与核心判稳的关系

后端返回的 `ready` 只是必要条件而非充分条件：

- `ready = False` 时，核心绝不判定为稳定；
- `ready = True`（或省略）后，核心仍会独立检查误差容限、斜率和稳定驻留时间（dwell）。
