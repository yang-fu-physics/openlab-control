# 读取、前面板与日志

System Instrument 有两种读取入口。多数仪表只需写 `poll()`；只有完整状态查询明显较慢时，
才需要额外写 `poll_measurement()`。

## `poll()`：完整状态

核心周期性调用 `poll()`。它应返回当前值、目标、速率、动作状态、仪表自己的稳定标志以及
需要长期监视的附加值。返回结果用于：

- 主窗口仪表卡片；
- 温度、磁场的稳定性判断；
- Live Trend；
- 每次 Run 的 `instrument_status.dat`；
- 报警与断线检测。

前面板默认约 1 秒更新一次。SEQ 判稳可以使用更短的内部周期，但不会让前面板跟着快速重绘。

```python
return InstrumentSnapshot(
    instrument_id=self.config.id,
    display_name=self.config.display_name,
    kind=self.config.kind,
    timestamp=time.monotonic(),
    connected=True,
    unit="K",
    current=temp_b,
    target=setpoint,
    rate_per_minute=ramp_rate,
    activity=InstrumentActivity.MOVING if ramping else InstrumentActivity.HOLDING,
    instrument_stable=not ramping,
)
```

核心会检查 ID、类型、有限数值、时间戳和附加列结构。不要用上次缓存值冒充刚读到的值。

## `poll_measurement()`：写 DAT 前的即时主值

Measurement Module 在真正需要温度或磁场的测量时刻调用 `api.instruments()`。核心会请求
一次即时快照，而不是复用最多一秒以前的前面板缓存。同一时刻多个模块的请求会合并。

默认实现直接调用 `poll()`，所以普通仪表不用写第二套代码。若完整查询需要很多条命令，才
覆盖 `poll_measurement()`，只读正式测量行需要的主值：

```python
async def poll_measurement(self):
    temp_b = read_sample_temperature(self._transport)
    return InstrumentSnapshot(
        instrument_id=self.config.id,
        display_name=self.config.display_name,
        kind=self.config.kind,
        timestamp=time.monotonic(),
        connected=True,
        unit="K",
        current=temp_b,
        # 本次没有同步读取的值留空，不能填上次缓存。
        target=None,
        rate_per_minute=None,
        metrics={
            "temp_a": InstrumentMetric("2nd Stage", None, "K", 3),
        },
    )
```

`poll()` 仍继续负责完整安全检查。`poll_measurement()` 不是跳过报警的办法；测量主值自身无效
或越过硬安全边界时仍应立即报错。

## 同一条通讯连接不会被并发访问

核心对每个 System Instrument 使用一个串行入口：

1. 已经开始的一条完整仪表指令先完成；
2. 等待中的控制和安全操作优先；
3. `poll_measurement()` 优先于尚未开始的普通 `poll()`；
4. 后台 `poll()` 最后执行。

“优先”不是强行打断一条已经发送到仪表的命令。底层 I/O 仍必须设置有限超时。后台代码也
不要另开线程直接使用同一个 VISA Session，否则会绕开这个顺序。

## 一台仪表返回多个值

同一温控仪可能同时返回样品温度、冷头温度、加热功率和加热量程。应保持一个连接，并把
辅助值放在有序 `metrics` 字典。不要先拼成一段字符串：

```python
metrics={
    "temp_a": InstrumentMetric("2nd Stage", temp_a, "K", 3),
    "heater_output": InstrumentMetric("Heater Output", heater_percent, "%FS", 2),
    "heater_range": InstrumentMetric("Heater Range", range_name),
}
```

- 字典键是固定列名，只能使用小写字母、数字和下划线；运行中不能增删、改名或改变顺序。
- `display_name` 只用于界面。
- `value` 可以是数值、文字、布尔值或 `None`。
- `None` 会写成空单元格，表示“本次没有读取”，不会沿用旧值。
- `decimals` 只控制显示和文件格式，不改变原始数值。

结构化字典比整段字符串多保留了数值类型、单位、顺序和精度。前面板会自动创建两列读数
格并随内容增加行；多台物理仪表的卡片会按窗口宽度自动换行。`message` 仍可用于显示一条
临时的人类说明，但不能替代需要写入 DAT、日志或参与报警的结构化读数。

## 两类数据文件

| 文件 | 何时写 | 用途 |
| --- | --- | --- |
| 实验 DAT | 每个 `Measure` 结果行 | 与模块测量结果同步的温度、磁场和即时附加值 |
| `instrument_status.dat` | Run 期间按独立周期 | 当前值、目标、速率、稳定、连接状态及完整附加读数 |

Data Browser 不会自动绑定当前实验 DAT；用户可以打开任意 DAT。Live Trend 使用已取得的
快照缓存，不会为了画图额外轮询仪表。

## 仪表稳定与框架稳定不是一回事

`instrument_stable` 保存仪表状态字。它为 `False` 时，框架不会宣告稳定；它为 `True` 时，
框架仍会独立检查目标误差、斜率和 dwell。这样可以避免一个错误或过早的状态位直接放行 SEQ。
