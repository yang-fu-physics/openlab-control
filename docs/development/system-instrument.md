# System Instrument 从这里开始

System Instrument 负责实验期间一直存在的仪表，例如主温控仪、主磁场控制器，以及一直显示的
冷头温度、压力或液位监视仪表。

电阻表、电压表、换向器和只在 `Measure` 时工作的测量组合属于 Measurement Module。

## 先判断写哪一种

| 需求 | 类型 |
| --- | --- |
| `Set Temperature` 或 `Scan Temperature` 控制它 | System Instrument |
| `Set Field` 或 `Scan Field` 控制它 | System Instrument |
| 程序一直显示，但从不设置 | System Instrument（`monitor`） |
| 只在 `Measure` 时读取 | Measurement Module |
| 有测量通道、设置窗口或模块 SEQ 指令 | Measurement Module |

System Instrument 通常随实验系统固定；Measurement Module 会按实验频繁 Enable 或 Disable。

## 运行流程

```text
configs/instruments.local.toml
        │ 地址 + System Instrument ID + 选中的辅助读数
        ▼
configs/site.local.toml 的 [[instruments]]
        │ resource + 控制许可 + 安全范围
        ▼
system_instruments/<id>/instrument.toml
        │ backend + 面板模板 + 主读数 + 所有读数的名称/单位/精度
        ▼
独立子进程 ── 串行访问 ── VISA / 串口 / 厂商库 ── 真实仪表
```

后端只有六个容易理解的方法：

- `open()`：连接并确认身份和初始状态；
- `read_status()`：完整状态，供前面板、安全检查、判稳和状态日志使用；
- `read_measurement()`：可选的快速主读数；不写时自动调用 `read_status()`；
- `set_target()`：可控温度或磁场仪表设置目标；
- `hold()`：用新鲜读数保持当前状态；
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

## 三份文件各管一件事

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

`configs/instruments.local.toml` 由扫描器生成，描述实验室里的实际仪表：

```toml
[[resources]]
id = "cryocon_main"
address = "USB0::...::INSTR"
purpose = "system"
system_instrument = "cryocon_22c_24c"
auxiliary_readings = ["temp_a"]
```

`configs/site.local.toml` 描述该实例是否可控和现场安全范围：

```toml
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

同一个信息只写一次：地址和实现选择在资源表；面板模板、主读数、单位和精度在清单；控制许可和安全
范围在现场主配置。外部 System Instrument 的 `[[instruments]]` 不再接受重复的 `backend`、
`unit`、旧 `role` 或仿真的 `initial_value`。清单中的未知字段也会直接标记为 Invalid。

## `kind` 与控制许可

- `temperature`：进入标准温度列，可控也可只读；
- `field`：进入标准磁场列，可控也可只读；
- `monitor`：只显示和记录，不能控制。

每种 temperature/field 最多一个 `control_enabled = true` 的实例。SEQ 自动选择它。没有启用
控制的 temperature/field 仍可显示和记录；monitor 永远不能启用控制。

`instrument.toml` 必须在 `[panel]` 中选择一个模板：`controller` 显示当前值、目标、速率和
稳定状态，并在获准控制时允许双击；`readout` 以 2×2 最多显示四个读数。选中的辅助读数
统一进入 `readout`；第五个读数开始使用右侧的下一个 `readout`。可控实例必须使用
`controller`，其主控制面板样式不因辅助读数改变。

## 阅读顺序

1. [写第一个 System Instrument](first-system-instrument.md)
2. [读取、前面板与日志](instrument-reading.md)
3. [控制与安全](instrument-control-safety.md)
4. [测试与现场接入](instrument-testing.md)

!!! warning "示例不是通用真实驱动"

    核心模板故意在连接真实仪表前报错。完成型号确认、有限 I/O 超时、现场上下限、写后
    回读、Hold、断线恢复和硬件联锁测试后，才能用于真实实验。
