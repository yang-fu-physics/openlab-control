# System Instrument 从这里开始

System Instrument 负责实验期间一直存在的仪表，例如主温控仪、主磁场控制器、压缩机开关，
以及持续显示的冷头温度、压力或液位监视仪表。

电阻表、电压表、换向器和只在 `Measure` 时工作的测量组合属于 Measurement Module。

## 先判断写哪一种

| 需求 | 类型 |
| --- | --- |
| `Set Temperature` 或 `Scan Temperature` 控制它 | System Instrument |
| `Set Field` 或 `Scan Field` 控制它 | System Instrument |
| 程序一直显示，但从不设置 | System Instrument（`monitor`） |
| 程序一直显示，并提供简单 On/Off 指令 | System Instrument（`monitor` + `switch`） |
| 只在 `Measure` 时读取 | Measurement Module |
| 有测量通道、设置窗口或模块 SEQ 指令 | Measurement Module |

System Instrument 通常随实验系统固定；Measurement Module 会按实验频繁 Enable 或 Disable。

## 作者模板与现场实例

两份文件承担不同职责：

```text
system_instruments/<id>/instrument.toml
        │ API v4 作者模板：字段、控制端点、固定面板、读数、指令
        │
        ▼ Instrument Scanner
configs/instruments/<id>.toml
        │ 一个或多个物理实例：连接值、面板启用、角色、顺序和限制
        ▼
每个物理实例一个独立子进程和通讯会话
```

`configs/general.toml` 只管应用、日志、报警、模块目录和 System 进程恢复参数，不保存实例。
未分配给 System Instrument 的 VISA 地址单独保存在 `configs/visa.resources.toml`，供
Measurement Module 使用。

扫描器从作者模板复制除 `panels` 外的静态元数据，再加入 `[[instances]]`。运行时仍以安装
目录中的 `panels` 作为固定模板；实例面板只引用稳定的 panel ID，不复制显示实现。

## 一个目录对应一种物理型号或协议

```text
system_instruments/my_temperature_controller/
├─ instrument.toml
├─ backend.py
├─ instrument.py          # 可选：底层命令与响应解析
└─ *.example.toml         # 可选：首次配置时复制的现场数据
```

不要按 TempA、TempB 或“主/副温度”拆目录。一台物理仪表可以返回多个读数、拥有多个面板和
控制端点，但它们共用一个实例、一个 worker 和一个连接。两台同型号物理仪表则在同一个
生成文件里建立两个实例，各自独立运行。

## API v4 清单

最小只读模板：

```toml
id = "my_monitor"
name = "My Monitor"
version = "0.1.0"
api_version = "4"
core_requires = ">=0.19,<0.20"
backend = "backend:MyMonitor"
kinds = ["monitor"]

[[panels]]
id = "reading"
label = "Reading"
template = "readout"
readings = ["value"]

[readings.value]
label = "Reading"
unit = "K"
decimals = 3
```

完整清单可由五组信息组成：

- `[[config_fields]]`：这一型号需要操作者填写的值；
- `[[controls]]`：后台可接受目标的控制端点；
- `[[panels]]`：固定面板模板；
- `[readings.<id>]`：读数名称、单位和小数位；
- `[[sequence_commands]]`：无参数 System 指令。

有 VISA 身份识别时再增加：

```toml
[discovery]
identity_pattern = "(?i)expected maker.*expected model"
```

扫描器用它标出匹配地址，正式 `open()` 仍须重新核对型号。专用网络仪表可以不声明发现
规则，而是在 `config_fields` 中提供自己的连接字段：

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

后台通过 `config.extras["host"]`、`config.extras["port"]` 读取这些值。VISA 实例的地址则
读取 `config.address`。

## 固定面板与角色

API v4 有四种面板模板：

- `controller`：控制端点、可选主读数、目标/速率限制与稳定参数；
- `readout`：一个只读值；
- `readout_grid`：一到四个只读值；
- `switch`：一个状态值和一个或多个已声明指令按钮。

温度控制面板示例：

```toml
[[controls]]
id = "main"
label = "Main Temperature"

[[panels]]
id = "control"
label = "Sample Temperature"
template = "controller"
control = "main"
reading_options = ["temp_a", "temp_b"]
default_reading = "temp_a"
min_value = 1.8
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 30.0
stability_tolerance = 0.01
stability_max_slope_per_minute = 0.01
stability_dwell_seconds = 5.0
stability_timeout_seconds = 1800.0
stability_window_seconds = 5.0
```

操作者可在扫描器中开启/关闭固定面板、选择 controller 读数、确认限制并调整全局顺序。
实例保存的 `role` 决定标准 SEQ 使用谁：

- `none`：仅显示、记录或提供指令；
- `sample_temp`：Temperature 与 Scan Temperature；
- `field`：Field 与 Scan Field。

`sample_temp` 和 `field` 各自全局最多一个，而且只能给支持对应 kind 的 `controller`。
只读与开关面板必须使用 `none`。没有任何 System 面板是有效状态，主程序可以正常启动。

一个物理实例的多个面板不会增加连接数。后端完整状态中的主值放在 `value`，其他读数放在
`auxiliary`；核心按照开启面板引用的读数生成显示和日志列。

### 多个独立控制回路 { #multiple-controls }

同一台物理仪表可以声明多个 `[[controls]]`，并让不同 `controller` 面板分别引用它们。
`set_target(..., control=<id>)` 与 `hold(control=<id>)` 中的 `control` 就是这里的稳定 ID；
后台据此选择真实 Loop，不能从面板标题猜测。

只有一个不同的已启用控制端点时，`read_status()` 可以继续在顶层返回
`target/rate/moving/ready`。启用了多个不同控制端点时，必须分别返回状态：

```python
return {
    "value": sample_temperature,
    "auxiliary": {"shield_temperature": shield_temperature},
    "controls": {
        "loop_1": {
            "target": sample_target,
            "rate": sample_rate,
            "moving": sample_ramping,
            "ready": sample_ready,
        },
        "loop_2": {
            "target": shield_target,
            "rate": shield_rate,
            "moving": shield_ramping,
            "ready": shield_ready,
        },
    },
}
```

每个回路的当前值仍取自对应面板选择的 `value` 或 `auxiliary` 读数，不在 `controls` 中
重复。核心按面板分别判稳、等待和记录；多个面板仍只打开一次物理连接。承担
`sample_temp` 或 `field` 角色的面板继续作为实验 DAT 中相应标准当前值和目标值的来源。

## 简单 System 指令

开关模板同时引用稳定的读数 ID 与指令 ID：

```toml
[[panels]]
id = "compressor"
label = "Compressor"
template = "switch"
reading = "compressor_state"
commands = ["compressor_on", "compressor_off"]

[readings.compressor_state]
label = "Compressor State"

[[sequence_commands]]
id = "compressor_on"
label = "Compressor On"

[[sequence_commands]]
id = "compressor_off"
label = "Compressor Off"
```

后台实现 `execute_sequence_command(command_id)`。同一指令会出现在面板按钮和右侧
**System Commands**，没有参数，也不生成 DAT 测量行。Warning 继续 SEQ，Error 中止。

## 后台方法

System Instrument 后台是普通同步类，常用方法是：

- `open()`：连接并确认身份和初始状态，不自动改变输出；
- `read_status()`：完整状态，供面板、安全检查、判稳和状态日志使用；
- `read_measurement()`：可选的快速即时读取；省略时调用 `read_status()`；
- `set_target(..., control=<id>)`：给某个控制端点设置目标；
- `hold(control=<id>)`：用新鲜读数保持当前状态；
- `execute_sequence_command()`：可选的无参数指令；
- `event_responses()`：可选的稳定事件响应声明；
- `close()`：可重复释放通讯，不擅自改变输出。

核心负责子进程、调用顺序、总超时、重连、时间戳和快照。后端仍须为每次底层 I/O 设置
有限超时，并在发送前复查目标和速率边界。写入超时后不能自动重发；重连后先读取实际状态。

## PID 文件

需要每台物理实例独立 PID 数据时，在 `config_fields` 使用 `type = "pid_file"` 并指定一个
示例文件。扫描器第一次保存时复制到 `configs/pid/<instance-id>.toml`，以后不会覆盖或删除。
作者可以让示例故意缺少现场值；例如空 `zones` 应由后台在连接前明确拒绝。

## 阅读顺序

1. [写第一个 System Instrument](first-system-instrument.md)
2. [读取、前面板与日志](instrument-reading.md)
3. [控制与安全](instrument-control-safety.md)
4. [测试与现场接入](instrument-testing.md)
5. [扫描并配置仪表](../guides/instrument-scanner.md)

!!! warning "示例不是通用真实驱动"

    核心模板故意在连接真实仪表前报错。完成型号确认、有限 I/O 超时、现场上下限、写后
    回读、Hold、断线恢复和硬件联锁测试后，才能用于真实实验。
