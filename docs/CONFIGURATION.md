# 配置参考

`configs/default.toml` 是可提交的仿真模板，不要把真实仪表地址、报警 Token 或实验室路径
直接写进去。部署真实仪表时先复制一份本机配置：

```powershell
Copy-Item .\configs\default.toml .\configs\site.local.toml
.\run.bat --config configs\site.local.toml
```

发布包使用 `OpenLabControl.exe --config configs\site.local.toml`。`configs/*.local.toml` 已被
Git 忽略；若团队需要共享模板，另存为脱敏的 `site.example.toml` 再提交。相对路径以配置
文件所在项目的根目录解析：配置位于 `configs/` 时，项目根目录是它的上一级。

真实 VISA 地址单独保存在 `configs/instruments.local.toml`。推荐用
`tools/instrument_scanner.py` 生成，不要把同一地址复制到主配置和多个模块设置中。

每次 Run 会把实际使用的主配置复制为 `configuration.toml`，并把已解析的资源表复制为
`instrument-resources.toml`。它们有助于复现实验，但包含真实地址和本机路径；分享、上传
或提交整个运行目录前必须检查并脱敏。

## `[application]`

| 键 | 默认值 | 说明 |
|---|---:|---|
| `title` | `OpenLab Control` | 主窗口标题 |
| `ui_scale` | `auto` | `auto` 或 0.75–2.0；用于 1080p/2K/4K 缩放 |
| `ui_refresh_ms` | `200` | GUI 消息刷新周期 |
| `poll_interval_seconds` | `1.0` | 前面板和常规状态的仪表轮询周期；测量时的即时采样不受此值限制 |
| `control_poll_interval_seconds` | `0.20` | SEQ 正在运行、暂停或停止收尾时的仪表判稳采样周期；不会提高前面板刷新率 |
| `simulation_speed` | `120.0` | 仿真控制器的时间倍率 |
| `default_sequence` | `examples/nested_scan.seq` | 启动时打开的 SEQ |
| `language` | `en_US` | 预留语言标识；当前 UI 以英文为主 |

`ui_scale = "auto"` 根据主屏原生分辨率和 DPI 选择缩放。手动值同时影响字体、固定宽高、
图标和窗口初始尺寸。它是现场默认值；用户在 **View → Appearance** 保存个人整体缩放后，
个人值优先。Appearance 还提供独立的 70%–150% 文字倍率和窗口布局记忆，这些值保存在
操作系统用户配置目录，不属于主配置，也不会进入运行快照。

Measurement Module 在测量时调用 `api.instruments()` 会请求一次即时仪表采样，不会复用最多
一个常规周期以前的前面板缓存，也不会永久改变 `poll_interval_seconds`。
System Instrument 实现可选 `read_measurement()` 后，这次采样可以只查询主测量值；未在该次
查询中读取的附加列写空，完整监视值仍由常规 `read_status()` 写入
`instrument_status.dat`。
SEQ 控制期间则使用 `control_poll_interval_seconds` 更新安全状态和稳定性；快照消息仍按
`poll_interval_seconds` 节流，所以界面不会因为内部判稳而快速闪动。

## `[logging]`

| 键 | 默认值 | 说明 |
|---|---|---|
| `directory` | `runs` | 自动运行目录根位置 |
| `data_file_name` | `experiment.dat` | 默认实验数据文件名 |
| `event_file_name` | `events.dat` | 事件文件名 |
| `instrument_status_file_name` | `instrument_status.dat` | 连续仪表状态文件名 |
| `instrument_status_interval_seconds` | `1.0` | Run 中仪表状态写入周期，必须大于 0 |
| `timestamp_epoch` | `labview_1904` | `labview_1904` 或 Unix 秒 |
| `flush_every_row` | `true` | 每写一行立即 Flush，降低断电损失 |
| `allow_external_paths` | `false` | 是否全局允许绝对/越界数据路径 |

`data_file_name`、`event_file_name` 与 `instrument_status_file_name` 必须是三个互不相同的
Windows 单文件名，不得包含目录、盘符、保留名、控制字符或尾随空格/句点。该限制防止
默认日志绕过自动运行目录，也避免三类文件互相覆盖。

推荐保持 `allow_external_paths = false`，由单条 `Set Datafile ... external ...` 明确授权
自定义目录。无论实验 DAT 选到哪里，SEQ、配置、仪表状态和模块快照始终保留在自动运行
目录。状态周期独立于 `poll_interval_seconds`；提高轮询频率不会自动增加状态文件写入量。

## `[abort]`

```toml
[abort]
temperature = "hold_current"
field = "hold_current"
```

- `hold_current`：Stop/Error 后读取并保持当前值。

当前版本只接受 `hold_current`，并同时作用于温度和磁场；其他值会在启动时被拒绝。测量模块在 SEQ 完成、Stop、Error 时收到 `on_event("run_end", {"reason": ...}, api)`；只有 Disable 和应用退出调用 `close(api)`。

## `[alarms]`

| 键 | 可选值/类型 | 说明 |
|---|---|---|
| `stability_timeout` | `info/warning/error` | 判稳超时级别，默认 `error` |
| `stale_reading` | `info/warning/error` | 读数过期级别 |
| `popup_warnings` | bool | Warning 是否弹窗 |
| `popup_errors` | bool | Error 是否弹窗 |

弹窗开关不影响事件记录或 SEQ 的 Error 中止语义。

### `[alarms.reporting]`

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

报警发射默认关闭。开启后只订阅已经去重的 Warning/Error 状态变化，不发送 Info、恢复
事件或同一 Source/Code/Context 尚未恢复时的重复报告。HTTP 在独立后台线程执行，
网络失败不会阻塞或改变 SEQ；连续失败在本地记录为 `ALARM_DELIVERY_FAILED` Warning，
之后任一报警投递成功会解除该 Warning。

发射端只发送 `event_id`、`level` 和 `message`。接收端按服务器配置选择 QQ：
Warning 仅测试员，Error 为管理员与测试员的并集。`event_id` 在 HTTP 重试期间保持
不变，配套 NoneBot 接收器可避免成功收件人收到重复消息。

Token 不应直接写进主 TOML，因为每次 Run 会保存配置快照。优先通过
`token_env` 指定的环境变量提供；也可把单行 Token 放入 `token_file` 指向的独立文件。
非本机地址默认必须使用 HTTPS；只有明确设置 `allow_insecure_http = true` 才允许远程
明文 HTTP，此时 Token 和报警正文可能被窃听。配套接收器位于
`integrations/nonebot_alarm_receiver/`，未配置 Token 时会 fail-closed。

## `[modules]`

```toml
[modules]
directory = "modules"
data_directory = "module_data"
state_directory = "trust_state"
shared_wheels_directory = "wheels"
python_executable = ""
runtime_directory = "runtime_packages"
startup_timeout_seconds = 10.0
operation_timeout_seconds = 120.0
shutdown_timeout_seconds = 3.0
```

| 键 | 说明 |
|---|---|
| `directory` | 启动/Refresh 扫描的模块源码根目录 |
| `data_directory` | 自动保存 `<module_id>/settings.toml` 的目录，必须与源码分离 |
| `state_directory` | 本机内容信任记录目录，不得作为共享配置提交 |
| `shared_wheels_directory` | 仅供模块额外依赖共用的离线 wheel 目录 |
| `python_executable` | 安装额外依赖时使用的 Python；源码运行留空即使用当前 Python |
| `runtime_directory` | 每模块额外依赖隔离 runtime 的根目录 |
| `startup_timeout_seconds` | 模块工作进程启动并完成源码加载的上限 |
| `operation_timeout_seconds` | open、configure、measure、event 等单次 IPC 操作的总上限 |
| `shutdown_timeout_seconds` | Disable/退出时模块 close + worker shutdown 的总上限 |

PySide6 6.11.1、QtAwesome 1.4.2、packaging 26.2、PyVISA 1.16.2 和
typing_extensions 4.16.0 是框架共享依赖，源码环境和正式 EXE 均直接提供。所有模块
默认使用这一组版本；manifest 中兼容的声明不会生成 runtime，也不会显示
`Install Dependencies`。

发布 EXE 不能把自身当作 pip。只有准备框架未提供的额外依赖时，才需要放置
`runtime/python/python.exe`，或把 `python_executable` 指向完全离线的便携 Python。
每个模块的额外依赖安装到：

```text
runtime_packages/module/<module-id>/<content-fingerprint>/site-packages/
```

额外依赖不进入 GUI/核心进程，也不能覆盖框架共享包；特殊模块仍可在自己的隔离目录中
使用不同的额外包版本。安装只读取模块 `requirements.lock`、模块 `wheels/` 和共享
`wheels/`；lock 中每一项必须是精确 `==` 版本并携带 SHA-256。程序固定使用
`--no-index --only-binary=:all: --require-hashes`，没有在线安装回退。

三个超时必须是大于零的有限秒数。框架超时是防止工作进程永久挂起的最终上限；真实
模块仍须为每次 VISA、串口、TCP 或 SDK 调用设置更短的协议超时。

## `[system_instruments]`

```toml
[system_instruments]
directory = "system_instruments"
resource_file = "configs/instruments.local.toml"
state_directory = "trust_state"
runtime_directory = "runtime_packages"
shared_wheels_directory = "wheels"
python_executable = ""
startup_timeout_seconds = 10.0
reconnect_timeout_seconds = 60.0
reconnect_interval_seconds = 2.0
```

| 键 | 说明 |
|---|---|
| `directory` | System Instrument 的手动安装目录 |
| `resource_file` | 仪表扫描工具生成的现场物理地址表；相对项目根目录解析 |
| `state_directory` | 本机内容指纹信任记录；不得作为共享配置提交 |
| `runtime_directory` | Instrument/Module 各自额外依赖的共同隔离根目录 |
| `shared_wheels_directory` | 可选的额外依赖离线 wheel 公共池 |
| `python_executable` | 为额外依赖准备的 Python；源码运行留空使用当前 Python |
| `startup_timeout_seconds` | 每个仪表工作进程启动/首次连接最终上限 |
| `reconnect_timeout_seconds` | 读链路失联后的总恢复窗，默认 60 秒 |
| `reconnect_interval_seconds` | 恢复窗内两次重新连接尝试之间的间隔 |

每个 System Instrument 的额外依赖路径为
`runtime_packages/instrument/<instrument-id>/<content-fingerprint>/site-packages/`。安装、哈希和
runtime 完整性规则与模块相同；框架共享依赖仍直接使用核心版本。

## 仪表资源表

`resource_file` 指向独立 TOML。每个条目是一台物理仪表，不是一个通道：

```toml
schema_version = 2

[[resources]]
id = "cryocon_main"
address = "USB0::0x1234::0x5678::SERIAL::INSTR"
identity = "Cryo-con,24C,SERIAL,1.0"
purpose = "system"
system_instrument = "cryocon_22c_24c"
auxiliary_readings = ["temp_a", "heater_output", "heater_range"]

[[resources]]
id = "keithley_2400_1"
address = "GPIB0::24::INSTR"
identity = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,SERIAL,1.0"
purpose = "measurement"
system_instrument = ""
auxiliary_readings = []
```

- `id` 是以后引用的稳定名称，地址变化时不需要改模块设置。
- `purpose` 只能是 `system` 或 `measurement`。
- System 资源必须选择已安装的 `system_instrument`。主读数由其清单固定；扫描器用复选框显示
  清单中的其他读数，并保存操作者选择的 `auxiliary_readings`。
- 同一地址不能登记两次；同一 System 资源也不能由两个 `[[instruments]]` 实例同时打开。
- Measurement Module 前端与后台都可用 `api.resources()` 取得深拷贝；System 地址不会暴露。
- 扫描只做资源枚举和一次 `*IDN?`，不会替用户设置上下限、PID、输出或模块参数。

## `[[instruments]]`

仪表只用于温度、磁场与只读 Monitor。外部 System Instrument 的条目通过 `resource` 自动
取得实现、地址、主读数、单位和精度：

| 键 | 必需 | 说明 |
|---|---|---|
| `id` | 是 | 全局唯一 ID，SEQ 通过它选择仪表；必须是非空可打印文本且不得有首尾空白，内部空格允许 |
| `display_name` | 是 | 英文 UI 名称 |
| `kind` | 是 | `temperature`、`field` 或 `monitor` |
| `resource` | 外部实现必需 | 资源表中的物理仪表 ID；内置仿真不得填写 |
| `backend` | 仅内置仿真 | `package.module:ClassName`；外部实现不得重复填写 |
| `control_enabled` | 否 | 默认 `false`；标准 SEQ 自动选择同 kind 中唯一的 `true` 实例 |
| `unit` | 仅内置仿真 | 外部实现的单位来自 `instrument.toml` |
| `initial_value` | 仅内置仿真 | 仿真初始值；真实 `resource` 条目填写该键会被拒绝 |
| `stale_after_seconds` | 否 | 读数超过该时间未更新视为 Stale，默认 3 秒 |
| `operation_timeout_seconds` | 否 | Open/Read/Set/Hold 的框架最终上限，默认 10 秒 |
| `shutdown_timeout_seconds` | 否 | Close 的框架最终上限，默认 3 秒 |

每个 temperature/field kind 最多一个 `control_enabled = true` 的实例。SEQ 自动选择它，
Run 前要求其已 Connected 且有新鲜读回。其他实例可以同时显示并保持只读。测量仪表应写成
完整 Measurement Module，而不是加入 `[[instruments]]`。

一台多通道温控仪只写一个条目。主样品温度由后端的 `value` 返回，TempA、加热功率、量程等通过
后端的 `auxiliary` 字典返回。多个不同物理仪表可以写多个条目；连接和轮询会并发进行。

仪表超时必须是大于零的有限秒数。读取/连接链路失败后，核心终止旧仪表进程并在
`system_instruments.reconnect_timeout_seconds` 内重建连接；运行中的主仪表恢复期间冻结 SEQ
活动计时。成功恢复后核对实际 target/rate，不重放写命令。Set/Hold 写超时是歧义
故障，立即 Faulted，不自动重试。真实驱动仍须设置更短的 VISA/串口/TCP/SDK 协议
超时；框架最终上限不能替代硬件互锁。

### 温度/磁场专用键

| 键 | 说明 |
|---|---|
| `default_rate_per_minute` | 新建 SEQ/手动控制的默认速率 |
| `min_value` / `max_value` | Target 硬限制；SEQ 弹窗与运行时共用 |
| `max_rate_per_minute` | 最大速率硬限制 |
| `stability_tolerance` | 当前值与目标值允许偏差 |
| `stability_max_slope_per_minute` | 判稳窗口最大绝对斜率 |
| `stability_dwell_seconds` | 同时满足偏差/斜率后需持续的时间 |
| `stability_timeout_seconds` | 本次目标的判稳超时 |
| `stability_window_seconds` | 计算斜率的窗口 |

`stability_timeout_seconds` 同时是 Settle 判稳和 Sweep 到达目标的最终等待上限；即使读取持续只返回 Warning、没有新快照，SEQ 也会按 `alarms.stability_timeout` 结束等待，不会无限挂起。

所有值使用仪表原生单位。默认磁场原生单位为 Oe：

```toml
[[instruments]]
id = "field"
display_name = "Magnetic Field"
kind = "field"
backend = "labcontrol.instruments.simulated:SimulatedFieldController"
control_enabled = true
unit = "Oe"
min_value = -90000.0
max_value = 90000.0
default_rate_per_minute = 5000.0
max_rate_per_minute = 10000.0
```

SEQ 仍可使用 T，中央会换算为仪表 Oe 后再检查上下限和速率。UI/SEQ 中 Oe 保留两位小数，T 保留六位，温度保留三位。

### Monitor

Monitor 是只读仪表，只需 `read_status()` 返回 `value`。它：

- 不接受 Set/Hold；
- 不参与标准温度/磁场自动选择或中央判稳；
- 在底部和 Live Trend 显示；
- 在每个模块结果行中由中央记录到 DAT；
- 可由 System Instrument 产生系统 Error，例如二级冷头过温。

默认示例：

```toml
[[instruments]]
id = "second_stage"
display_name = "2nd Stage"
kind = "monitor"
backend = "labcontrol.instruments.simulated:SimulatedReadOnlyMonitor"
unit = "K"
initial_value = 4.2
noise = 0.002
```

## System Instrument 自定义键

未被框架识别的仪表键进入 `InstrumentConfig.extras`，例如仿真 `noise`，或具体型号的
`baud_rate`、`termination`、PID 表等。物理地址是例外：真实外部 System Instrument 必须
使用 `resource` 引用资源表，主配置中的内联 `address` 会被拒绝。密码、令牌和私钥也不得
提交到仓库。

Measurement Module 的设置不放在主配置，而由其自定义 Settings UI 管理并保存到
`module_data/<id>/settings.toml`。保存 SEQ 时，当前实验关联值还会复制到同目录同名
`<sequence>.modules.toml`，使不同 SEQ 可以携带不同的模块参数。Load 只把值装入界面，
不会自动 Enable 或 Apply；因此主配置的仪表安全限制和模块显式确认流程不会被绕过。

## 配置验证

启动会拒绝：

- 无仪表条目、重复仪表 ID，或空白/控制字符导致无法寻址的仪表 ID；
- 未知仪表 kind；
- 外部 System Instrument 未选择资源、主配置内联物理地址，或内置仿真占用真实资源；
- 同一种类多个可控仪表、monitor 启用控制，或仍使用已经删除的 `role`；
- `min_value >= max_value`；
- 非正默认/最大速率；
- `ui_scale` 越界；
- 无法解析的严重 TOML 错误。

模块清单错误不会阻止主程序启动；对应行显示 Invalid/说明并禁止 Enable，便于修复其他仪表或模块。
