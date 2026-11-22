# 编写 System Instrument（系统仪表）从这里开始

**System Instrument** 负责实验期间常驻的底层环境设备：主温控仪、超导磁体电源、压缩机开关，以及一直挂在界面上显示的冷头温度、液位或压力监视器。

> 如果你只是要接入电阻、电压、电流或锁相测量，请直接看 [Measurement Module 教程](index.md)，不需要读本章。

## 1. 一张表确认它是不是 System Instrument

| 你的需求 | 应该写成什么 |
| --- | --- |
| 接受 `Set / Scan Temperature` 或 `Set / Scan Field` 控制 | System Instrument（`controller` 面板） |
| 界面一直显示某个温度/压力/液位，从不控制 | System Instrument（`readout` / `readout_grid` 面板） |
| 界面常驻显示开关状态，并提供简单 `On / Off` 按钮与 SEQ 指令 | System Instrument（`switch` 面板） |
| 只在 `Measure` 时采集数据，或需要设置页、多通道测量 | Measurement Module |

## 2. 两个文件分工：作者模板 vs 现场实例

写驱动时只管维护型号模板，实验室现场的 IP、GPIB 地址、面板顺序和限值都交给 **Instrument Scanner** 生成：

```text
system_instruments/<型号ID>/instrument.toml
        │ 作者模板：这台仪表有哪些字段、控制回路、固定面板、读数和指令
        ▼  通过 Instrument Scanner 配置
configs/instruments/<型号ID>.toml
        │ 现场实例：1 台或多台物理设备的地址、角色、顺序与现场安全上下限
        ▼  启动主程序
每个物理实例自动在独立子进程中运行（共用一个通讯会话）
```

> **重要组织原则**：一个目录对应**一种物理仪表型号/协议**，不要按 `TempA`、`TempB` 或“主/副温度”拆成两个目录。同一台温控仪的样品温度、冷头温度、加热输出都由**同一个实例、同一个连接**读回，再展示到一个或多个面板上。如果实验室有两台同型号温控仪，则在扫描器里添加两个 `[[instances]]` 即可，每台自动拥有独立子进程。

## 3. 目录结构

```text
system_instruments/my_temperature_controller/
├─ instrument.toml        # API v4 清单（扫描器据此自动生成配置表单）
├─ backend.py             # 继承 SystemInstrument 的同步后台类
├─ instrument.py          # 可选：底层 SCPI / Modbus 命令与解析
└─ pid.example.toml       # 可选：首次配置时自动复制给实例的 PID 模板
```

## 4. 看懂 `instrument.toml`（API v4 清单）

最简只读监视仪表的清单只有不到 20 行：

```toml
id = "my_monitor"
name = "My Monitor"
version = "0.1.0"
api_version = "4"
core_requires = ">=0.19,<0.21"
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

完整清单由五组标准区块构成（全部按需声明）：

- **`[[config_fields]]`**：需要操作员在扫描器里填写的型号专用字段（如超时、网络 `host`/`port`、通道顺序、PID 文件等），后台通过 `config.extras["字段ID"]` 读取。支持类型：`string`、`integer`、`number`、`boolean`、`choice`、`ordered_choices`（可选 `allow_empty = true` 和 `option_count_from`）、`pid_file`。
- **`[discovery]`**：VISA 仪表可声明 `identity_pattern` 正则，扫描器会用它标出匹配地址（正式 `open()` 仍须重新核对型号）；非 VISA 网口仪表不写此块，直接用 `config_fields` 声明 `host`/`port`。
- **`[[controls]]`**：这台仪表有几个可控制回路（例如单回路 `main`，或双回路 `loop_1` / `loop_2`）。
- **`[[panels]]`**：驱动提供的固定面板模板，共有五种：
  - `controller`：绑定一个控制端点、主读数、默认上下限、默认/最大速率与判稳参数；
  - `readout`：显示 1 个只读读数；
  - `readout_grid`：以 2×2 网格显示 1–4 个只读读数；
  - `switch`：显示 1 个状态值（0/1 → Off/On）并绑定无参数控制按钮；
  - `custom`：可选自定义底部卡片，通过 `widget = "frontend:ClassName"`（以及可选 `readings = [...]`）加载驱动目录下的自定义 `QWidget`（在扫描器中同样可自由勾选启用与排序）。
- **`setup_widget`**（顶层可选字段）：指向驱动目录下的自定义配置界面（如 `setup_widget = "frontend:SetupWidget"`），由主菜单 **Instrument → Instrument Setup…** 及仪表卡片右键菜单打开，适合曲线切换、软件 PID 表切换等低频配置，不占用底部卡片位且不在后台周期轮询。
- **`[readings.<id>]`**：集中声明每个读数的名称、单位和小数位。
- **`[[sequence_commands]]`**：可选的无参数系统指令（如 `Compressor On` / `Compressor Off`），会自动出现在 `switch` 面板和右侧 **System Commands** 中。

### 面板角色（`role`）

操作员在扫描器里决定每个面板是否启用、全局排列顺序以及角色：

- `sample_temp`：接管标准 `Set / Scan Temperature`（全局最多 1 个，仅限支持 `temperature` 的 `controller`）；
- `field`：接管标准 `Set / Scan Field`（全局最多 1 个，仅限支持 `field` 的 `controller`）；
- `none`：只显示、记录、手动控制或响应按钮指令（可任意多个）。

### 同一台仪表有多个控制回路 { #multiple-controls }

一台物理仪表若同时控制多个回路（如两路独立温控），可在清单中声明多个 `[[controls]]` 和对应的 `controller` 面板。调用 `set_target(..., control=<id>)` 和 `hold(control=<id>)` 时，核心会传入稳定的 control ID。

- 只有一个已启用控制回路时，`read_status()` 可以直接把 `target / rate / moving / ready` 写在顶层；
- 启用了多个不同控制回路时，必须按 control ID 放进 `controls` 字典分别返回：

```python
return {
    "value": sample_temperature,
    "auxiliary": {"shield_temperature": shield_temperature},
    "controls": {
        "loop_1": {"target": sample_target, "rate": sample_rate, "moving": sample_ramping, "ready": sample_ready},
        "loop_2": {"target": shield_target, "rate": shield_rate, "moving": shield_ramping, "ready": shield_ready},
    },
}
```

每个回路的当前值仍取自该面板绑定的 `value` 或 `auxiliary` 读数；核心会为每个回路独立判稳、等待并在 `instrument_status.dat` 中分别记录，所有面板始终只占用一个物理连接。

## 5. 后台只需要写普通同步 Python

继承 `labcontrol.instruments.base.SystemInstrument` 即可，子进程隔离、串行排队、超时保护、断线重连和快照生成全部由核心代劳：

- `open()`：连接并核对型号、单位和初始状态（**不改变仪表输出**）；
- `read_status()`：返回当前主值、附加读数和控制状态字典；
- `read_measurement()`（可选）：写 DAT 测量行前的快速即时读取，省略时默认调用 `read_status()`；
- `set_target(value, rate_per_minute, mode="Settle", *, control)`：校验限值后下发目标并回读确认；
- `hold(*, control)`：用同一次操作里现读的新鲜值保持当前状态；
- `execute_sequence_command(command_id)`（可选）：执行无参数系统指令；
- `execute_action(action, payload=None)`（可选）：响应自定义卡片或 `setup_widget` 通过 `InstrumentUIAPI.call_action(...)` 发起的异步 JSON 请求；写动作（`read_only=False`）仅在 SEQ Idle 时允许执行并记入 `events.dat`。若返回 `{"config_updates": {...}}`，核心会校验键属于 `config_fields` 并原子写回 `configs/instruments/<id>.toml`（如持久化切换后的 `pid_file`）；
- `event_responses()`（可选）：声明由核心执行的跨仪表安全联动；
- `close()`：幂等释放通讯句柄（不擅自改写仪表设定）。

### 可选自定义卡片与 Instrument Setup 前端（`frontend.py`）

- 自定义 `QWidget` 的 `__init__(self, api: InstrumentUIAPI)` 只通过受限的 `InstrumentUIAPI`（`call_action`、`config`、`snapshot`、`editable`、`open_setup`）与后台通信，绝不直接触碰 VISA/串口句柄；
- `template = "custom"` 的卡片需实现 `update_snapshot(snapshot)`（可选实现 `set_actions_enabled(enabled)`）；
- `setup_widget` 可选实现 `on_activated()`（仅在打开窗口或切到该 Tab 时按需向仪表查询）、`update_snapshot(snapshot)` 和 `set_editable(editable)`。

## 推荐学习顺序

1. [写第一个 System Instrument](first-system-instrument.md) — 从最安全的只读温度计开始
2. [读取、前面板与日志](instrument-reading.md) — 理解状态字典、附加读数和即时测量读取
3. [控制与安全](instrument-control-safety.md) — 限值校验、写后回读、Hold、断线重连与事件联动
4. [测试与现场接入](instrument-testing.md) — 假连接协议测试与分阶段真机验证
5. [扫描并配置仪表](../guides/instrument-scanner.md) — 用向导把新驱动配进实验台

!!! warning "内置示例不是真实驱动"

    `system_instruments/example_controller` 和 `example_monitor` 只是默认报错的教学骨架，不能直接用于真实设备。
