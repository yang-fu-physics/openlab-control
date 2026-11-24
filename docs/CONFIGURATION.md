# 配置参考

OpenLab Control 的配置按用途拆成五类，彼此清晰隔离：

```text
configs/
├─ general.toml                         # 通用配置（手工维护，随仓库提供）
├─ visa.resources.toml                  # 留给测量模块的 VISA 清单（扫描器生成）
├─ module_instances.toml                # 可选的测量模块多实例配置（Modules 窗口管理）
├─ instruments/<仪表ID>.toml            # 系统仪表实例、面板顺序与限值（扫描器生成）
└─ pid/<实例ID>.toml                    # 单台温控实例自己的 PID 表（首次复制，后续保留）
```

> 全新安装时只需有 `configs/general.toml` 即可正常启动（此时没有系统仪表面板，三个内置仿真默认关闭）。
> 每次运行 SEQ 时，程序会自动把当次实际使用的配置快照保存到 `runs/<运行目录>/configuration/`；对外分享前请注意检查并脱敏其中的本机地址、路径与现场 PID。

---

## 1. 通用配置：`configs/general.toml`

### `[application]`（界面、轮询与默认序列）

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `title` | `"OpenLab Control"` | 主窗口标题 |
| `ui_scale` | `"auto"` | 整体缩放：`auto` 或 `0.75`–`2.0`（用户在 **View → Appearance** 保存的个人缩放优先） |
| `ui_refresh_ms` | `200` | GUI 消息轮询刷新周期（毫秒） |
| `poll_interval_seconds` | `1.0` | 前面板显示与常规状态轮询周期（秒） |
| `control_poll_interval_seconds` | `0.20` | SEQ 控温/控场与判稳期间的采样周期（秒）；前面板刷新仍按 `poll_interval_seconds` 节流 |
| `simulation_speed` | `120.0` | 内置仿真控制器的时间加速倍率 |
| `default_sequence` | `"examples/nested_scan.seq"` | 启动时默认打开的 SEQ 路径 |
| `language` | `"en_US"` | 语言标识（当前界面控件以英文为主） |

### `[logging]`（数据与日志）

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `directory` | `"runs"` | 自动运行目录的根目录 |
| `data_file_name` | `"experiment.dat"` | 默认实验主数据文件名 |
| `event_file_name` | `"events.dat"` | 事件日志文件名 |
| `instrument_status_file_name` | `"instrument_status.dat"` | 连续仪表状态日志文件名 |
| `instrument_status_interval_seconds` | `1.0` | Run 期间状态日志写入间隔（秒） |
| `timestamp_epoch` | `"labview_1904"` | 绝对时间起点：`labview_1904` 或 `unix` |
| `flush_every_row` | `true` | 每写一行立即刷盘，降低意外断电丢数风险 |
| `allow_external_paths` | `false` | 是否默认允许把 DAT 写到项目目录外（建议保持 `false`，按需在单条 `Set Datafile` 使用 `external`） |
| `compact_measurement_data` | `true`（发行配置） | `true` 时实验 DAT 只写 `Timestamp`、主控温度 `Temp` 与模块列，目标/加热器等状态保存在 `instrument_status.dat`；设为 `false` 切回兼容宽表（详见 [DAT 格式](DAT_FORMAT.md)） |

三个日志文件名必须互不相同，且为纯文件名（不能包含目录或盘符）。

### `[alarms]` 与 `[alarms.reporting]`（告警与可选推送）

| 键 | 可选值 / 默认值 | 说明 |
| --- | --- | --- |
| `stability_timeout` | `warning`（保留兼容字段） | 判稳超时在运行时统一按 Warning 提示并继续等待，不中止 SEQ（仅驱动明确报告 `moving` 时暂停超时计时） |
| `stale_reading` | `info / warning / error` | 仪表读数过期告警级别 |
| `popup_warnings` / `popup_errors` | `bool` | 是否弹窗提示（关闭弹窗不改变日志记录与 Error 停机语义） |

```toml
[alarms.reporting]
enabled = false
endpoint = "http://127.0.0.1:3889/alarm/report"
token_env = "OPENLAB_ALARM_TOKEN"
token_file = ""
target_qq = ""
device_name = "OpenLab Control"
timeout_seconds = 3.0
retry_attempts = 3
retry_delay_seconds = 1.0
queue_size = 100
shutdown_timeout_seconds = 2.0
allow_insecure_http = false
```

远程报警默认关闭；`target_qq` 为空时始终关闭。QQ 非空时须为正整数数字串，并将该值作为 `target_qq` 随 Warning/Error 请求发送。`device_name` 是通知中的设备名称，消息格式为 `[设备名][级别][来源]：实际错误内容`（例如 `[2K Hall][ERROR][cryocon22c]：...`）。Token 通过 `X-Token` 请求头传递。

GUI 顶部 **Settings → Alarm Notifications…** 可设置启用、服务地址、Token 和接收 QQ，保存后立即生效，下次启动保留。GUI 的本机 `alarms.json` 覆盖 TOML 中的报警配置，Token 单独保存在同目录 `alarm-token.txt`；这些文件位于用户配置目录，不复制到运行归档。无界面模式仍读取上述 TOML。

发送运行在独立后台线程中，只发送去重后的状态变化；非本机地址默认要求 HTTPS，使用 HTTP 须显式勾选 Allow HTTP 或设置 `allow_insecure_http = true`。投递失败产生本地 Warning，不阻塞实验。

### `[modules]`（测量模块超时与目录）

```toml
[modules]
directory = "modules"
data_directory = "module_data"
startup_timeout_seconds = 10.0
operation_timeout_seconds = 120.0
shutdown_timeout_seconds = 3.0
```

- `directory` / `data_directory`：模块源码目录与保存 Settings 的目录；
- `startup_timeout_seconds`：子进程启动并加载模块的总上限；
- `operation_timeout_seconds`：`open`、`configure`、`measure`、`on_event` 等单次调用的总上限；
- `shutdown_timeout_seconds`：`close` 与子进程回收的总上限。

### `[system_instruments]`（系统仪表目录与重连）

```toml
[system_instruments]
directory = "system_instruments"
startup_timeout_seconds = 10.0
reconnect_timeout_seconds = 60.0
reconnect_interval_seconds = 2.0
```

- 普通读取断线后，按 `reconnect_interval_seconds` 自动重试直至 `reconnect_timeout_seconds`；连回后核对实际目标/速率，绝不自动重发写命令。写入超时属于结果不确定，会立即停机。

---

## 2. 测量模块 VISA 资源表：`configs/visa.resources.toml`

由 Instrument Scanner 生成，仅保存**未分配给系统仪表**的 VISA 资源，供测量模块选择：

```toml
[[resources]]
id = "keithley_2400_1"
address = "GPIB0::24::INSTR"
identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,..."
```

- `id`：稳定资源 ID（小写字母开头，只含小写字母、数字和下划线），模块设置只保存这个 ID；
- `address`：实际 VISA 地址，比较时忽略大小写，全局唯一；
- `identity`：扫描时 `*IDN?` 的原始回复（允许为空）。

---

## 3. 系统仪表实例：`configs/instruments/<仪表ID>.toml`

每种已配置的系统仪表对应一个文件，文件名与顶层 `id` 必须与 `system_instruments/<仪表ID>/instrument.toml` 一致。同一个文件可包含多个 `[[instances]]`（多台同型号设备）：

```toml
id = "lakeshore340"
name = "Lake Shore Model 340 Temperature Controller"
version = "0.2.0"
api_version = "4"
backend = "backend:LakeShore340"
kinds = ["temperature"]

[[config_fields]]
id = "visa_timeout_ms"
label = "VISA I/O Timeout (ms)"
type = "integer"
default = 1000
min = 1

[[controls]]
id = "main"
label = "Main Temperature"

[discovery]
identity_pattern = "^LSCI,MODEL340(?:,|$)"

[readings.temp_a]
label = "Temp A"
unit = "K"
decimals = 3

[[instances]]
id = "sample_controller"
resource = "GPIB0::12::INSTR"
identity = "LSCI,MODEL340,..."
visa_timeout_ms = 1000

[[instances.panels]]
id = "control"
enabled = true
order = 1
role = "sample_temp"
reading = "temp_a"
min_value = 1.8
max_value = 400.0
default_rate_per_minute = 1.0
max_rate_per_minute = 30.0
stability_tolerance = 0.01
stability_max_slope_per_minute = 0.01
stability_dwell_seconds = 5.0
stability_timeout_seconds = 1800.0
stability_window_seconds = 5.0

[[instances.panels]]
id = "temperatures"
enabled = true
order = 2
role = "none"
```

规则要点：

- 实例只能启用/关闭作者清单中声明好的固定面板；
- 全部已启用面板的全局 `order` 必须从 1 开始连续且不重复；
- `role` 只能是 `none`、`sample_temp` 或 `field`，后两者全局各最多 1 个，且只能赋给支持对应类型的 `controller` 面板。

---

## 4. 独立 PID 文件：`configs/pid/<实例ID>.toml`

当温控仪清单包含 `type = "pid_file"` 字段时，扫描器在**首次保存**该实例时把模板（或操作者选定的已验证文件）复制到 `configs/pid/<实例ID>.toml`。此后无论重新保存还是删除实例，扫描器都不会覆盖或删除该文件。

---

## 5. 保存与启动校验

Instrument Scanner 确认保存时按顺序原子执行：创建缺失的新 PID 文件 → 原子重写本次预览中的系统仪表配置 → 替换 `visa.resources.toml` → 删除预览中标为 `DELETE` 的旧生成文件。OpenLab Control 启动前还会再次校验 ID、地址冲突、面板顺序、角色唯一性与范围边界，任何不合法配置都会在接触真实硬件前停止加载。
