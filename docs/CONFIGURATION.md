# 配置参考

默认配置是 `configs/default.toml`。相对路径以配置文件所在项目的根目录解析：默认配置位于 `configs/`，因此项目根目录是它的上一级。修改真实设备前先复制配置并纳入版本管理。

## `[application]`

| 键 | 默认值 | 说明 |
|---|---:|---|
| `title` | `OpenLab Control` | 主窗口标题 |
| `ui_scale` | `auto` | `auto` 或 0.75–2.0；用于 1080p/2K/4K 缩放 |
| `ui_refresh_ms` | `200` | GUI 消息刷新周期 |
| `poll_interval_seconds` | `0.20` | 控制/Monitor 轮询周期 |
| `simulation_speed` | `120.0` | 仿真控制器的时间倍率 |
| `default_sequence` | `examples/nested_scan.seq` | 启动时打开的 SEQ |
| `language` | `en_US` | 预留语言标识；当前 UI 以英文为主 |

`ui_scale = "auto"` 根据主屏原生分辨率和 DPI 选择缩放。手动值同时影响字体、固定宽高、图标和窗口初始尺寸。

## `[logging]`

| 键 | 默认值 | 说明 |
|---|---|---|
| `directory` | `runs` | 自动运行目录根位置 |
| `data_file_name` | `experiment.dat` | 默认实验数据文件名 |
| `event_file_name` | `events.dat` | 事件文件名 |
| `timestamp_epoch` | `labview_1904` | `labview_1904` 或 Unix 秒 |
| `flush_every_row` | `true` | 每写一行立即 Flush，降低断电损失 |
| `allow_external_paths` | `false` | 是否全局允许绝对/越界数据路径 |

推荐保持 `allow_external_paths = false`，由单条 `Set Datafile ... external ...` 明确授权自定义目录。无论实验 DAT 选到哪里，SEQ、配置和模块快照始终保留在自动运行目录。

## `[abort]`

```toml
[abort]
temperature = "hold_current"
field = "hold_current"
```

- `hold_current`：Stop/Error 后读取并保持当前值。
- `keep_target`：保留原 Target。

该配置只作用于温度和磁场。测量模块在 SEQ 完成、Stop、Error 时调用 `end_sequence(reason)`；只有 Disable 和应用退出调用 `abort()`。

## `[alarms]`

| 键 | 可选值/类型 | 说明 |
|---|---|---|
| `stability_timeout` | `info/warning/error` | 判稳超时级别，默认 `error` |
| `stale_reading` | `info/warning/error` | 读数过期级别 |
| `popup_warnings` | bool | Warning 是否弹窗 |
| `popup_errors` | bool | Error 是否弹窗 |

弹窗开关不影响事件记录或 SEQ 的 Error 中止语义。

## `[modules]`

```toml
[modules]
directory = "modules"
data_directory = "module_data"
shared_wheels_directory = "wheels"
python_executable = ""
site_packages_directory = "module_runtime/site-packages"
```

| 键 | 说明 |
|---|---|
| `directory` | 启动/Refresh 扫描的模块源码根目录 |
| `data_directory` | 自动保存 `<module_id>/settings.toml` 的目录，必须与源码分离 |
| `shared_wheels_directory` | 所有模块共用的离线 wheel 目录 |
| `python_executable` | 安装依赖时使用的 Python；源码运行留空即使用当前 Python |
| `site_packages_directory` | pip `--target` 的共享依赖目录，主进程和所有模块工作进程共用 |

发布 EXE 不能把自身当作 pip。需要安装额外依赖时，可放置 `runtime/python/python.exe`，或把 `python_executable` 指向便携 Python。依赖始终安装到 `site_packages_directory`，不会为每个模块复制一套环境。

如果不同模块对同一包声明不相容的版本范围，相关模块都禁止 Enable。若依赖缺失，先在 Modules Manager 选择模块并点击 `Install Dependencies`：程序先从共享 `wheels/` 和模块自己的 `wheels/` 离线安装；离线失败后，只有用户再次明确确认才允许在线 pip。

## `[[devices]]`

设备只用于温度、磁场与只读 Monitor。每个条目必需：

| 键 | 必需 | 说明 |
|---|---|---|
| `id` | 是 | 全局唯一 ID，SEQ 通过它选择设备 |
| `display_name` | 是 | 英文 UI 名称 |
| `kind` | 是 | `temperature`、`field` 或 `monitor` |
| `plugin` | 是 | `package.module:ClassName` |
| `unit` | 否 | 原生单位 |
| `initial_value` | 仿真 | 初始值 |

旧 `kind = "measurement"` 不再支持。测量仪表应改写为 `modules/<id>/` 下的完整 Measurement Module。

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
| `stale_after_seconds` | 读数超过该时间未更新视为 Stale |

所有值使用设备原生单位。默认磁场原生单位为 Oe：

```toml
[[devices]]
id = "field"
display_name = "Magnetic Field"
kind = "field"
plugin = "labcontrol.devices.simulated:SimulatedFieldController"
unit = "Oe"
min_value = -90000.0
max_value = 90000.0
default_rate_per_minute = 5000.0
max_rate_per_minute = 10000.0
```

SEQ 仍可使用 T，中央会换算为设备 Oe 后再检查上下限和速率。UI/SEQ 中 Oe 保留两位小数，T 保留六位，温度保留三位。

### Monitor

Monitor 是只读单值设备，只需 Poll 返回 `current`。它：

- 不接受 Set/Hold；
- 不参与标准温度/磁场自动选择或中央判稳；
- 在底部和 Live Trend 显示；
- 在每个模块结果行中由中央记录到 DAT；
- 可用于设备插件产生系统 Error，例如二级冷头过温。

默认示例：

```toml
[[devices]]
id = "second_stage"
display_name = "2nd Stage"
kind = "monitor"
plugin = "labcontrol.devices.simulated:SimulatedReadOnlyMonitor"
unit = "K"
initial_value = 4.2
noise = 0.002
```

## 插件自定义键

未被框架识别的设备键进入 `DeviceConfig.extras`，例如仿真 `noise`。真实插件可在此放 address、baud rate、termination 等，但密码、令牌和私钥不得提交到仓库。

Measurement Module 的设置不放在主配置，而由其自定义 Settings UI 管理并保存到 `module_data/<id>/settings.toml`。

## 配置验证

启动会拒绝：

- 无设备条目或重复设备 ID；
- 未知设备 kind；
- `min_value >= max_value`；
- 非正默认/最大速率；
- `ui_scale` 越界；
- 无法解析的严重 TOML 错误。

模块清单错误不会阻止主程序启动；对应行显示 Invalid/说明并禁止 Enable，便于修复其他设备或模块。
