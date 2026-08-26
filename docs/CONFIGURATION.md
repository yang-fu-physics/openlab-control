# 配置参考

OpenLab Control 的配置分为四类。只有 `configs/general.toml` 是通用设置；仪表相关文件由
Instrument Scanner 生成或初始化：

```text
configs/
├─ general.toml                         # 唯一通用配置
├─ visa.resources.toml                  # 未分配的 VISA，供 Measurement Module 使用
├─ instruments/<instrument-id>.toml     # System Instrument 实例与面板选择
└─ pid/<instance-id>.toml               # 某个物理实例自己的 PID 数据
```

直接运行 `run.bat` 或 `OpenLabControl.exe` 时会读取 `configs/general.toml`。它不保存物理仪表
地址、System 面板或 PID 数据。全新安装可以没有 `visa.resources.toml`、`instruments/` 和
`pid/`；此时没有 System Instrument 面板，程序仍能启动。三个内置仿真也都默认关闭，需在
Instrument Scanner 最后一页明确勾选。

每次 Run 会在运行目录建立 `configuration/` 快照，按原目录结构保存本次实际使用的
`general.toml`、存在时的 `visa.resources.toml`、`instruments/*.toml` 和 `pid/*.toml`。
其中可能包含真实地址、本机路径和现场 PID；分享运行目录前必须检查并脱敏。

## `configs/general.toml`

### `[application]`

| 键 | 默认值 | 说明 |
| --- | ---: | --- |
| `title` | `OpenLab Control` | 主窗口标题 |
| `ui_scale` | `auto` | `auto` 或 0.75–2.0；现场界面缩放 |
| `ui_refresh_ms` | `200` | GUI 消息刷新周期 |
| `poll_interval_seconds` | `1.0` | 前面板和常规状态的轮询周期 |
| `control_poll_interval_seconds` | `0.20` | SEQ 控制和判稳时的采样周期 |
| `simulation_speed` | `120.0` | 仿真控制器的时间倍率 |
| `default_sequence` | `examples/nested_scan.seq` | 启动时打开的 SEQ |
| `language` | `en_US` | 语言标识；当前界面以英文为主 |

`ui_scale = "auto"` 根据主屏分辨率和 DPI 选择缩放。用户在 **View → Appearance** 保存的
个人设置优先，并保存在 Windows 用户配置目录，不写入这里。

Measurement Module 调用 `api.instruments()` 时会请求一次即时仪表采样，不会把常规前面板
缓存冒充同步测量值。System Instrument 可实现较快的 `read_measurement()`；未在该次请求中
读取的附加列写空。SEQ 控制使用 `control_poll_interval_seconds` 做安全检查和判稳，但界面
快照仍按 `poll_interval_seconds` 节流。

### `[logging]`

| 键 | 默认值 | 说明 |
| --- | ---: | --- |
| `directory` | `runs` | 自动运行目录根位置 |
| `data_file_name` | `experiment.dat` | 实验数据文件名 |
| `event_file_name` | `events.dat` | 事件文件名 |
| `instrument_status_file_name` | `instrument_status.dat` | 连续仪表状态文件名 |
| `instrument_status_interval_seconds` | `1.0` | Run 中状态写入周期 |
| `timestamp_epoch` | `labview_1904` | `labview_1904` 或 `unix` |
| `flush_every_row` | `true` | 每行立即刷新，降低断电损失 |
| `allow_external_paths` | `false` | 是否允许数据文件写到项目目录外 |

三个日志文件名必须互不相同，并且是普通 Windows 文件名，不能包含目录或盘符。推荐保持
`allow_external_paths = false`，仅在单条 `Set Datafile` 中明确授权外部目录。

### `[alarms]`

| 键 | 可选值 | 说明 |
| --- | --- | --- |
| `stability_timeout` | `info/warning/error` | 判稳超时级别 |
| `stale_reading` | `info/warning/error` | 读数过期级别 |
| `popup_warnings` | bool | Warning 是否弹窗 |
| `popup_errors` | bool | Error 是否弹窗 |

弹窗开关不改变事件记录或 Error 中止 SEQ 的语义。

#### `[alarms.reporting]`

```toml
[alarms.reporting]
enabled = false
endpoint = "http://127.0.0.1:3889/alarm/report"
token_env = "OPENLAB_ALARM_TOKEN"
token_file = ""
timeout_seconds = 3.0
retry_attempts = 3
retry_delay_seconds = 1.0
queue_size = 100
shutdown_timeout_seconds = 2.0
allow_insecure_http = false
```

报警发射默认关闭。开启后只发送已经去重的 Warning/Error 状态变化；网络工作位于独立线程，
失败会记录本地 Warning，但不会阻塞或改变 SEQ。Token 优先放在 `token_env` 指定的环境变量
中，也可放在 `token_file` 指向的单行文件中。非本机 HTTP 地址必须使用 HTTPS，除非明确
允许不安全的明文连接。

### `[modules]`

```toml
[modules]
directory = "modules"
data_directory = "module_data"
startup_timeout_seconds = 10.0
operation_timeout_seconds = 120.0
shutdown_timeout_seconds = 3.0
```

| 键 | 说明 |
| --- | --- |
| `directory` | 启动和 Refresh 时查找 Measurement Module 的目录 |
| `data_directory` | 自动保存 `<module-id>/settings.toml` 的目录 |
| `startup_timeout_seconds` | 模块进程启动并加载源码的总上限 |
| `operation_timeout_seconds` | open、configure、measure、event 等一次调用的总上限 |
| `shutdown_timeout_seconds` | close 与 worker shutdown 的总上限 |

这些值必须是有限正数。真实模块仍要给每次 VISA、串口、网络或 SDK 调用设置更短的协议
超时。模块与 System Instrument 共用核心锁定依赖，不在运行中安装另一套 Python 环境。

### `[system_instruments]`

```toml
[system_instruments]
directory = "system_instruments"
startup_timeout_seconds = 10.0
reconnect_timeout_seconds = 60.0
reconnect_interval_seconds = 2.0
```

| 键 | 说明 |
| --- | --- |
| `directory` | System Instrument 手动安装目录 |
| `startup_timeout_seconds` | 每个仪表进程启动和首次连接的总上限 |
| `reconnect_timeout_seconds` | 普通读链路失联后的总恢复窗口 |
| `reconnect_interval_seconds` | 两次重连尝试之间的间隔 |

恢复成功后程序读取仪表的实际 target/rate，不重放写命令。写入超时属于结果不确定的故障，
会立即停止，不自动重试。

## `configs/visa.resources.toml`

这个文件只保存尚未分配给 System Instrument 的 VISA 地址，供 Measurement Module 在设置窗口
中选择。每条记录只有三个字段：

```toml
[[resources]]
id = "keithley_2400_1"
address = "GPIB0::24::INSTR"
identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,..."
```

- `id` 是模块长期保存的稳定名称，格式为小写字母、数字和下划线，并以字母开头。
- `address` 是 VISA 资源地址；比较时忽略大小写，同一地址只能出现一次。
- `identity` 是扫描时得到的完整 `*IDN?` 文本；没有响应时可为空。

System Instrument 选中一个 VISA 地址后，该地址写入它自己的实例，并从本文件移除。因此
System Instrument 与 Measurement Module 不会同时打开同一个 VISA 会话。曾经保存、但本次
扫描没有发现的 Measurement 资源会以灰色卡片显示，并默认保留；取消 **Keep for
Measurement Module** 才会删除。

## `configs/instruments/<instrument-id>.toml`

每个文件对应一种已安装的 System Instrument 模板，可以有多个 `[[instances]]`，即多台同
型号物理仪表。文件名、顶层 `id` 和 `system_instruments/<id>/instrument.toml` 的 ID 必须
一致。

Instrument Scanner 从作者的 API v4 清单复制静态元数据，例如 `config_fields`、`controls`、
`readings`、`discovery` 和 `sequence_commands`，但不复制 `panels` 模板。每个实例只保存
物理连接字段、型号专用配置和对固定面板 ID 的选择。例如：

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

上例只展示一部分静态读数，实际生成文件会包含清单中除面板模板外的完整静态元数据。实例
字段由清单的 `config_fields` 决定；有 `discovery.identity_pattern` 的模板还会保存
`resource` 和 `identity`。没有 VISA 发现规则的专用网络仪表可在自己的 `config_fields` 中
声明 `host`、`port` 等输入，扫描器会在该仪表页面显示这些字段。

实例面板不能增加作者未声明的模板。每个模板都必须出现一次：关闭时只保存 `id` 和
`enabled = false`；开启时再保存全局 `order` 与 `role`。`controller` 还保存 `reading`、
上下限、速率与稳定参数。`readout`、`readout_grid` 和 `switch` 的角色必须是 `none`。

角色只有：

- `none`：显示、记录或提供面板指令，但不接管温场 SEQ；
- `sample_temp`：供 Temperature/Scan Temperature 使用，必须属于支持 temperature 的
  `controller`；
- `field`：供 Field/Scan Field 使用，必须属于支持 field 的 `controller`。

所有已开启面板的 `order` 必须从 1 开始连续且全局不重复。`sample_temp` 和 `field` 各自
全局最多一个。`none` 可以重复。没有任何生成文件或没有开启面板都是有效配置。

扫描器保存的是最后预览中的完整结果：它原子覆盖将保留的生成文件，并删除预览中标为
DELETE 的其他 `configs/instruments/*.toml`。不要把它当作局部追加工具；保存前应检查完整
预览和面板顺序。

## `configs/pid/<instance-id>.toml`

当作者把某个 `config_fields` 项声明为 `type = "pid_file"` 时，第一次保存实例会把清单指定
的示例文件复制为 `configs/pid/<instance-id>.toml`。第一次也可以选择另一份已经验证的 TOML
作为复制来源。生成的实例只保存这一目标路径。

目标 PID 文件一旦存在，扫描器会禁用来源选择；以后保存或移除实例都不会覆盖或删除它。
这让每台物理仪表保留独立的现场 PID 数据。某些作者示例会故意使用 `zones = []`；例如
Cryo-con 22C/24C 会在连接前拒绝空 `zones`，必须先填入该冷却系统已经验证的区间。

## 保存与启动验证

Instrument Scanner 的最后一页会显示 CREATE、OVERWRITE、UNCHANGED、DELETE 和 PID 复制动作。
确认后才执行完整保存：

1. 新 PID 文件先创建，现有 PID 文件保持不动；
2. 所有选中的 System Instrument 生成文件原子写入；
3. `visa.resources.toml` 以当前 Measurement 选择完整替换；
4. 未出现在预览中的生成文件删除。

OpenLab Control 启动时还会严格检查 ID、未知字段、VISA 地址重复、面板顺序、角色唯一性、
读数引用、数值范围和 PID 文件。失败发生在连接真实仪表之前，不会猜测或补全配置。
