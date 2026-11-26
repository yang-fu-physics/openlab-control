# DAT 与事件日志格式

OpenLab Control 的全部实验数据文件均为纯文本逗号分隔（CSV）格式，包含 `[Header]` 与 `[Data]` 两个区段，方便用 **Data Browser**、Python、Origin 或 Excel 直接打开。只有核心的 `DatRunLogger` 负责写文件，模块不会直接操作磁盘。

## 每次 Run 生成哪些文件？

```text
runs/20260723_120000_nested_scan/
├─ experiment.dat                        # 实验主测量数据
├─ instrument_status.dat                 # 每秒连续仪表状态记录
├─ events.dat                            # Warning / Error / Remark 事件日志
├─ sequence.seq                          # 本次运行的 SEQ 快照（含 T/F 状态）
├─ configuration/                        # 当次使用的 general.toml、visa、module_instances、instruments、pid 快照
├─ module_settings/
│  ├─ simulated_transport.settings.toml       # 本次模块界面期望值
│  └─ simulated_transport.status-at-start.json# Run 开始前读回的模块实际状态
└─ rawdata/                              # 仅当模块提交原始序列时出现
   └─ experiment__<摘要>__<模块ID>.rawdata
```

如果 SEQ 用 `Set Datafile ... external` 把实验 DAT 写到自定义文件夹，其余快照和日志仍保留在本次运行目录中。

---

## 1. 实验主数据（`experiment.dat`）

文件头示例：

```text
[Header]
; OpenLab Control Data File (default extension .dat)
BYAPP,OpenLab Control,0.20.9
INFO,...

[Data]
Timestamp,Temp,Delta_R1(ohm),Delta_R1_StdDev(ohm),...,Delta_Current,Delta_StatusCode
...
```

### 紧凑表（默认发行模式：`compact_measurement_data = true`）

为了让数据表干净、易画图，默认只写入：

1. **`Timestamp`**：绝对时间戳（默认 `labview_1904`，可在 `general.toml` 改为 `unix`）；
2. **`Temp`**：角色为 `sample_temp` 的主控当前温度（未配置主温控时省略此列）；
3. **所有已启用测量模块声明的列**：
   - 单个基础模块直接使用模块声明名（如 `Resistance(Ohm)`、`Delta_R1(ohm)`），不加冗长前缀；
   - 通过 **Add Instance…** 创建的复用实例自动带上简短实例名前缀 `<实例标签>.`：若实例 ID 形如 `<模板ID>_<名称>` 且后缀以字母开头，则直接取后缀作为标签（如 `keithley_2400_gate` → `gate.Resistance(Ohm)`、`gate.Voltage(V)`；`keithley_2400_sample` → `sample.Resistance(Ohm)`），否则使用完整实例 ID（如 `keithley_2400_2.Resistance(Ohm)`）；
   - 若多个基础模块或不同模板实例仍出现同名列（或标签冲突），核心自动回退为 `<模块ID>.<列名>`（如 `keithley_2400.StatusCode`、`lakeshore_372a.StatusCode`），确保每列唯一且来源明确。

目标温度、加热功率、磁场目标、二级冷头温度等完整系统状态统一保存在旁边的 `instrument_status.dat`。

### 兼容宽表（`compact_measurement_data = false`）

如需把所有系统状态一起写进主 DAT，可关闭紧凑模式，此时额外包含：

- `Timestamp(s)`、`Time(s)`（从 Run 创建起的单调耗时）、`SequenceStep`（当前嵌套步骤路径）；
- `Temp(K)`、`TempTarget(K)`、`Field(Oe)`、`FieldTarget(Oe)` 及各附加监控列；
- 模块列自动带 `<模块ID>.` 前缀（如 `simulated_transport.R1(Ohm)`）。

### 多通道、空单元格与状态码

- 多通道模块按逻辑通道（`slots`）逐行输出，每行只填当前通道测得的字段，其他通道单元格留空：
  ```text
  ... R1=101.2, R2=,      R3=,      R4=,      StatusCode=0
  ... R1=,      R2=102.5, R3=,      R4=,      StatusCode=0
  ```
- 状态列统一记录整数（通常 `0` 表示正常，非零含义见各模块 README），不写 `"Error"` 等文字；测量无效时省略无效数值列，仅留状态码。
- 数值格式：温度默认 3 位小数，磁场 `Oe` 2 位、`T` 6 位，模块浮点数最多 9 位有效数字。

### 原始采样文件（`rawdata/`）

当模块返回 `(row, raw_values)` 时，核心校验全为有限数值（每行最多 32,768 点）后写入 `rawdata/<dat名>__<10位路径摘要>__<模块ID>.rawdata`：无表头、每行纯逗号分隔数字，与主 DAT 中该模块出现的行顺序严格一一对应。

---

## 2. 事件日志（`events.dat`）

```text
[Header]
; OpenLab Control Event Log

[Events]
Timestamp(s),ISO8601,Severity,Source,Code,State,Count,Context,Message
```

| 列 | 说明 |
| --- | --- |
| `Severity` | `info` / `warning` / `error` |
| `Source` | `sequence`、仪表 ID、`module:<id>`、`logging` 等 |
| `Code` / `Context` | 稳定错误码与去重上下文（`Source + Code + Context` 相同则累计 `Count` 不重复弹窗） |
| `State` | `RAISED`（触发）或 `RESOLVED`（恢复） |
| `Message` | 人类可读说明 |

---

## 3. 连续仪表状态日志（`instrument_status.dat`）

如需自选路径、正整数秒间隔和按仪表分页选择记录项，使用 [System Log](guides/system-log.md)：默认 30 s，手动开始／停止，空闲和 SEQ 期间均可记录已有缓存，同名文件续写。

Run 开始时立即建立并先写一行初始快照，随后默认每秒写一行（只在 Run 期间写入，不受打开 Live Trend 影响）：

- 每个启用的 `controller` 面板按 `<仪表ID>.<面板ID>.*` 记录：`Current`、`Target`、`Rate`、`Activity`、`Stability`、`Ready`；
- 每台物理实例按 `<仪表ID>.*` 记录一次共享物理状态：`Connection`、`ReadingAge(s)`、`Message` 及所有附加读数（冷头温度、加热输出、量程等）。

---

## 4. `.plt` 视图伴随文件与 Python 读取

Data Browser 点击 **Save PLT** 时在 DAT 同目录保存同名 `.plt`，仅记录 X/Y 列、Overlay/Stacked、对数坐标和缩放范围，不含测量数据。

用 Python 快速读取任意 OpenLab DAT：

```python
from pathlib import Path
from labcontrol.dat_reader import read_dat

table = read_dat(Path("runs/.../experiment.dat"))
print(table.columns)
for point in table.numeric_points("Timestamp", "Delta_R1(ohm)"):
    print(point.x, point.y, point.source_row)
```
