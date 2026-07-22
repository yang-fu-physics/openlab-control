# 配置参考

默认文件：`configs/default.toml`

配置采用 TOML，程序启动时一次性读取。每次运行会复制一份到运行目录。

## `[application]`

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | string | 主窗口标题 |
| `ui_scale` | `"auto"` 或 float | 自动按屏幕原生分辨率缩放，或用 `0.75` 到 `2.0` 手动覆盖 |
| `ui_refresh_ms` | integer | GUI 清空后台消息队列的周期 |
| `poll_interval_seconds` | float | 设备轮询周期 |
| `simulation_speed` | float | 仅仿真 Ramp 的时间倍率；真实插件必须忽略 |
| `default_sequence` | string | 相对项目根目录的启动 SEQ |
| `language` | string | 界面语言标识；当前默认 `en_US`，界面以英文为主 |

默认 `ui_scale = "auto"`。程序把 Qt 报告的逻辑尺寸乘以设备像素比得到原生像素尺寸，再计算保守倍率，因此 Windows DPI 缩放和界面倍率可以共同工作。典型结果为：

| 原生分辨率 | 自动倍率 | 全局基准字号 |
|---|---:|---:|
| 1366×768 / 1920×1080 | 1.00× | 10 pt |
| 2560×1440 | 1.15× | 11.5 pt |
| 3840×2160 | 1.40× | 14 pt |

如果自动结果不符合屏幕尺寸或观看距离，可直接写入固定值，例如：

```toml
[application]
ui_scale = 1.4
```

修改后重新启动程序。状态栏和 About 窗口会显示实际倍率与 Auto/Manual 模式。

## `[logging]`

| 字段 | 类型 | 默认行为 |
|---|---|---|
| `directory` | string | 每次运行目录的父目录 |
| `data_file_name` | string | 未执行 Set Datafile 时的数据文件名 |
| `event_file_name` | string | 事件文件名 |
| `timestamp_epoch` | string | `labview_1904` 或 `unix` |
| `sparse_channel_rows` | bool | true 时每个通道单独一行，其他通道留空 |
| `flush_every_row` | bool | 每行写入后刷新文件 |
| `allow_external_paths` | bool | 是否允许 SEQ 写项目运行目录以外的绝对路径 |

为兼容用户模板，默认采用 LabVIEW 1904 时间基准和稀疏通道行。

## `[abort]`

字段：

- `temperature`
- `field`

可用策略：

- `hold_current`：调用插件 `hold()`，默认应把设备目标改为中止瞬间的当前值。
- `keep_target`：不发新命令，让设备保持原目标和原动作。

真实插件必须在文档中明确 `hold()` 对应的厂商命令和失败处理。

## `[alarms]`

| 字段 | 可选值 | 说明 |
|---|---|---|
| `stability_timeout` | info/warning/error | 判稳超时的级别；通常使用 error |
| `stale_reading` | info/warning/error | 预留的数据陈旧策略 |
| `popup_warnings` | bool | Warning 首次发生时是否弹窗 |
| `popup_errors` | bool | Error 首次发生时是否弹窗 |

即使关闭弹窗，事件仍会写入日志。Error 仍会中止序列。

## `[[devices]]`

每个设备是一个数组项。

公共字段：

| 字段 | 必需 | 说明 |
|---|---|---|
| `id` | 是 | 全局唯一，SEQ 通过它引用设备 |
| `display_name` | 是 | 界面状态块标题 |
| `kind` | 是 | temperature、field、measurement 或 monitor |
| `plugin` | 是 | `python.module:ClassName` |
| `unit` | 否 | 显示和插件原生单位 |
| `initial_value` | 控制型/Monitor | 仿真初值 |
| `default_rate_per_minute` | 控制型 | 默认速率 |
| `min_value` | 控制型 | 最小允许目标 |
| `max_value` | 控制型 | 最大允许目标 |
| `max_rate_per_minute` | 控制型 | 最大允许速率 |
| `channels` | 测量型 | DAT 通道顺序 |

`monitor` 是只读单值设备。它只需要返回 `current`，不创建稳定性算法，不接受 Target/Hold/Measure，不会被温度或磁场 SEQ 自动选择。默认 `2nd Stage` 配置为：

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

该 Monitor 目前只显示并进入 Live Trend，不增加 DAT 数据列，也不参与主温度判稳。

稳定性字段，仅控制型设备使用：

| 字段 | 说明 |
|---|---|
| `stability_tolerance` | 当前值与目标值的最大允许误差 |
| `stability_max_slope_per_minute` | 滑动窗口斜率绝对值上限 |
| `stability_dwell_seconds` | 连续满足条件的时间 |
| `stability_timeout_seconds` | 从目标改变到超时的时间 |
| `stability_window_seconds` | 斜率拟合窗口 |
| `stale_after_seconds` | 无新数据后判定陈旧的时间 |

未被框架识别的设备字段会原样放入 `DeviceConfig.extras`，供插件读取。例如：

```toml
[[devices]]
id = "my_meter"
display_name = "组合表"
kind = "measurement"
plugin = "labcontrol_plugins.my_meter:MyMeter"
unit = "Ohm"
channels = ["Rxx", "Rxy"]
visa_resource = "GPIB0::12::INSTR"
timeout_ms = 5000
retry_count = 2
```

插件中读取：

```python
resource = self.config.extras["visa_resource"]
timeout_ms = int(self.config.extras.get("timeout_ms", 5000))
```

## 单位

- 温度基础支持 K。
- 默认磁场设备原生单位为 Oe，基础层仍支持 T 和 Oe，换算关系为 `1 T = 10000 Oe`。
- 速率单位与对应控制量单位每分钟一致。
- 测量通道单位来自设备的 `unit`。

默认磁场配置如下。范围、速率、误差和斜率阈值全部以设备原生 Oe 表示；它们与旧版 ±9 T、1 T/min、0.002 T 和 0.001 T/min 的物理含义相同：

```toml
[[devices]]
id = "field"
unit = "Oe"
default_rate_per_minute = 5000.0
min_value = -90000.0
max_value = 90000.0
max_rate_per_minute = 10000.0
stability_tolerance = 20.0
stability_max_slope_per_minute = 10.0
```

界面、SEQ 新命令和框架生成的 DAT 对 Oe 使用两位小数，对 K 温度使用三位小数。若硬件插件的厂商协议使用 T，优先在插件内部换算；也可把设备原生单位配置为 T，此时兼容显示使用六位小数。

真实插件应以配置声明的单位与框架交换数值。厂商协议使用其他单位时，在插件内部转换。

## 推荐配置管理

- `default.toml` 保存实验室安全默认值。
- 每套硬件复制一份独立配置，例如 `configs/cryostat_a.toml`。
- 不在配置中存密码、令牌或网络凭据。
- 修改安全限制需要代码审查或双人核对。
- 运行目录中的配置快照只用于追溯，不应反向覆盖当前配置。
