# DAT 与事件格式

OpenLab Control 使用带 `[Header]` / `[Data]` 段的逗号分隔 DAT。中央框架是实验 DAT 的唯一写入者；Measurement Module 只能声明列并发出行数据。

## 运行目录

默认结构：

```text
runs/20260723_120000_nested_scan/
├─ sequence.seq
├─ configuration.toml
├─ module_settings/
│  ├─ simulated_transport.settings.toml
│  └─ simulated_transport.status-at-start.json
├─ experiment.dat
└─ events.dat
```

- `sequence.seq`：实际执行文档快照，包括 T/F 状态。
- `configuration.toml`：主配置完整副本。
- `*.settings.toml`：本次 Run 时模块 Settings 页的期望值。
- `*.status-at-start.json`：Run 开始前从模块后台读取的实际状态。
- 只为 Enabled 模块生成设置/状态快照。

若 SEQ 使用 external `Set Datafile`，实验 DAT 可在自定义目录，但其他文件仍在自动运行目录。

## 实验 DAT

最小结构：

```text
[Header]
; OpenLab Control Data File (default extension .dat)
BYAPP,OpenLab Control,0.10.2
INFO,...

[Data]
Timestamp(s),Time(s),SequenceStep,Temp(K),TempTarget(K),Field(Oe),FieldTarget(Oe),second_stage(K),simulated_transport.R1(Ohm),...,simulated_transport.Status,simulated_transport.Warning
...
```

### 时间列

- `Timestamp(s)`：默认是从 1904-01-01 UTC 起的秒数，与用户模板/LabVIEW 习惯兼容；`timestamp_epoch = "unix"` 时改为 Unix 秒。
- `Time(s)`：从本次 Run 创建开始的单调经过时间，不受系统时钟校准影响。
- `SequenceStep`：完整嵌套路径，例如 `1:Scan Temperature ... / point 2/3=... / 1:Measure`。

### 系统状态列

默认单温控/单磁体时：

- `Temp(K)`、`TempTarget(K)`：三位小数；
- `Field(Oe)`、`FieldTarget(Oe)`：Oe 两位小数；
- `<monitor_id>(<unit>)`：例如 `second_stage(K)`，默认三位小数。

若配置多个 temperature 或 field，为保持列唯一，会使用：

```text
sample_temp.Temp(K)
sample_temp.TempTarget(K)
main_magnet.Field(Oe)
main_magnet.FieldTarget(Oe)
```

每一行模块结果到达时，中央立即复制当时最新的系统快照。R1–R4 顺序测量因此可以拥有不同的温度、磁场和二级冷头温度。

### 模块列

模块在 `module.toml` 中声明固定列：

```toml
[[columns]]
name = "R1"
unit = "Ohm"

[[columns]]
name = "Status"
```

运行时自动生成：

```text
simulated_transport.R1(Ohm)
simulated_transport.Status
```

前缀是模块 ID，不是显示名；这样多个模块都声明 `Voltage` 或 `Status` 也不会冲突。Run 开始后 Schema 固定，直到该 Run 结束。

模块每次 `emit_row()` 只需提供本行有效值。其他已声明列以及其他模块的全部列留空。例如示例模块一次 Measure 依次写：

```text
... R1=<value>, R2=,       R3=,       R4=,       Status=OK, Warning=
... R1=,       R2=<value>, R3=,       R4=,       Status=OK, Warning=
... R1=,       R2=,       R3=<value>, R4=,       Status=OK, Warning=
... R1=,       R2=,       R3=,       R4=<value>, Status=OK, Warning=
```

模块应自行声明业务状态和告警列。框架不会再添加通用 `Module Status` 或 `Warning Code` 列。

### 空模块与失败行

- Measure 时没有 Enabled 模块：写一行只有系统状态的行，模块列不存在，并产生 Warning。
- 所有模块都未发出有效行：中央补一行系统状态，避免该 Measure 在 DAT 中完全消失。
- 某模块 Warning：已经发出的有效值照常写；无效测量值可留空；详细告警写 events.dat。
- 某模块 Error：其他并发模块在 Error 收束前已发出的行保留；若没有任何行则仍保留系统状态行，然后 SEQ Faulted。

### 值格式

- 温度固定三位；Oe 固定两位；T 固定六位。
- 模块 float 使用最多 9 位有效数字。
- 模块可写数字、bool、字符串或空值；复杂对象会触发 Schema/类型 Error。
- CSV 会自动引用含逗号或引号的文本。

## `events.dat`

结构：

```text
[Header]
; OpenLab Control Event Log

[Events]
Timestamp(s),ISO8601,Severity,Source,Code,State,Count,Context,Message
```

| 列 | 含义 |
|---|---|
| `Severity` | `info`、`warning`、`error` |
| `Source` | `sequence`、设备 ID、`module:<id>`、`logging` 等 |
| `Code` | 稳定机器可读代码 |
| `State` | `RAISED` 或 `RESOLVED` |
| `Count` | 同一活动事件重复报告次数 |
| `Context` | 通道/地址/操作等去重上下文 |
| `Message` | 英文用户可读说明 |

活动事件键为 Source+Code+Context。重复报告只增加 Count，不重复弹窗；恢复时写 RESOLVED。Info 不锁存。模块手动动作成功也写 Info，但不会写实验 DAT。

## 写入保证

- 默认每行 Flush。
- 同一模块的行顺序保持。
- 多模块结果按中央收到顺序串行写入，没有两个进程同时写同一文件。
- Error/Stop/完成都会在 `end_sequence()` 结束后关闭文件。
- 异常断电仍可能损失操作系统未落盘缓存；重要实验建议使用 UPS 和磁盘级备份。

## Data Browser 读取规则

Data Browser 与当前 Run 不绑定：

- 打开或拖入哪个 DAT 就显示哪个；
- 文件大小/修改时间改变时自动重读；
- 短行补空，重复列名在读取层安全重命名；
- 右键一次勾选多个 Y 后统一确认；
- Overlay 可在同图显示多个 Y，Stacked 可让多图共享 X；
- X/Y 可独立启用 Log；非正数据在 Log 模式不绘制；
- 框选放大，双击最近数据点查看原始行全部字段。

## `.plt` 显示伴随文件

对 `sample.dat` 的显示设置保存在同目录 `sample.plt`。它与实验数据分离，只记录：

- X 列；
- 多个 Y 列及 Overlay/Stacked 布局；
- X/Y Linear/Log；
- 缩放范围和标记显示。

如果原 DAT 更新，图会更新但继续使用 `.plt` 中可用的列配置；列不存在时安全回退，不修改 DAT。

## Python 读取示例

```python
from pathlib import Path
from labcontrol.dat_reader import read_dat

table = read_dat(Path("runs/.../experiment.dat"))
print(table.columns)
for point in table.numeric_points("Time(s)", "simulated_transport.R1(Ohm)"):
    print(point.x, point.y, point.source_row)
```
