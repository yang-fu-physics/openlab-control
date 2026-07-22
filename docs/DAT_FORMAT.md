# DAT 与事件格式

`examples/template_original.dat` 是本项目收到的原始 DAT 模板的逐字节副本；`examples/template_excerpt.dat` 是便于阅读的小型摘录。实现以原始模板中的 `[Header]`、`[Data]`、LabVIEW 1904 时间戳和稀疏通道行结构为兼容基线。

## 测量数据文件

默认文件：`experiment.dat`

结构：

```text
[Header]
; comments
BYAPP,OpenLab Control,0.8.1
INFO,...

[Data]
Timestamp(s),Time(s),SequenceStep,...
<rows>
```

文件是 UTF-8 文本，数据区域使用标准 CSV 转义规则。虽然扩展名为 `.dat`，字段由逗号分隔。

默认 `2nd Stage` 使用 `kind = "monitor"`，本阶段是界面只读值，因此只在 Header 的设备配置说明中出现，不增加 `[Data]` 列，也不作为测量或判稳标准。若后续需要把辅助温度作为正式数据通道记录，应按设备插件工作流明确列名和记录语义后再启用，而不是把它伪装成主 `temperature`。

## 时间戳

为兼容用户模板，默认 `Timestamp(s)` 是从 1904-01-01 00:00:00 UTC 起的秒数：

```text
labview_timestamp = unix_timestamp + 2082844800
```

`Time(s)` 是本次运行开始后的单调时钟秒数，不受系统时间调整影响。

将配置中的 `timestamp_epoch` 改为 `unix` 可输出 Unix 时间戳，但这可能破坏现有分析脚本兼容性。

## 列

固定列：

- `Timestamp(s)`
- `Time(s)`
- `SequenceStep`

每个控制设备增加：

- 当前值，例如 `Temp(K)`、`Field(T)`。
- 目标值，例如 `TempTarget(K)`、`FieldTarget(T)`。

每个测量设备按配置顺序增加通道，例如：

- `R1(Ohm)`
- `R2(Ohm)`
- `R3(Ohm)`
- `R4(Ohm)`

## 稀疏通道行

模板一次只填充一个电阻通道，其他列为空。本框架默认保持这一行为：

```text
...,300.0016,300,0,0,0.92318,,,
...,300.0017,300,0,0,,0.55070,,
```

设置：

```toml
sparse_channel_rows = false
```

后，每次 Measure 将所有通道写入同一行。

## 数据文件路径

每次运行先建立唯一运行目录。Set Datafile 可修改文件名：

- 相对路径在运行目录内解析。
- 默认不允许向运行目录外写绝对路径。
- 被重定向的路径产生 `DATAFILE_RELOCATED` Warning。

## 事件文件

默认文件：`events.dat`

结构：

```text
[Header]
; OpenLab Control Event Log

[Events]
Timestamp(s),ISO8601,Severity,Source,Code,State,Count,Context,Message
```

字段：

| 字段 | 说明 |
|---|---|
| `Timestamp(s)` | 与数据文件相同的绝对时间基准 |
| `ISO8601` | UTC 可读时间 |
| `Severity` | info、warning、error |
| `Source` | sequence、logging、runtime 或设备 ID |
| `Code` | 稳定的机器可读事件代码 |
| `State` | RAISED 或 RESOLVED |
| `Count` | 该活动周期内重复报告次数 |
| `Context` | 通道、设备、路径或 SEQ 步骤 |
| `Message` | 人类可读信息 |

## 写入保证

- 默认每个数据行后刷新。
- 每个事件立即刷新。
- CSV writer 负责包含逗号和引号的字段转义。
- 程序正常关闭时显式刷新并关闭。
- 操作系统或电源突然故障仍可能造成最后一个文件系统缓存块丢失；关键实验应使用可靠电源和存储。

## Python 读取示例

```python
import csv
from pathlib import Path

path = Path("experiment.dat")
lines = path.read_text(encoding="utf-8").splitlines()
start = lines.index("[Data]") + 1
rows = list(csv.DictReader(lines[start:]))
```

不要直接把整个文件交给普通 CSV 读取器；先定位 `[Data]`。

## Data Browser 读取规则

独立 Data Browser 使用 `src/labcontrol/dat_reader.py`，不依赖当前 Sequence、运行目录或测量设备：

- 定位第一个大小写不敏感的 `[Data]` 行。
- 下一条非空、非注释 CSV 记录作为列名。
- 支持 UTF-8 BOM、UTF-16 和 GB18030。
- 短行自动补空单元格，额外字段生成 `Extra N` 列，重复列名增加 `#2` 等后缀。
- 只有至少包含一个可解析有限数值的列才出现在坐标轴菜单中。
- 空字符串和非数值字段不作为数据点，也不会被替换成零。
- 横轴可使用任意数值列或从 1 开始的 `Row Number`。

浏览器只读文件，不会修改、锁定或重命名被浏览的 DAT。自动刷新检查修改时间和文件大小；若文件在读取期间继续追加，实际已读取字节数会使下一轮再次刷新，避免漏掉文件尾部。

## PLT 显示格式伴随文件

Data Browser 把显示设置写在 DAT 同目录、同主文件名的 `.plt` 文件中：

```text
C:\data\sample.dat  ->  C:\data\sample.plt
```

规范文件是 UTF-8 JSON：

```json
{
  "format": "OpenLab Control Plot Format",
  "version": 2,
  "data_file": "sample.dat",
  "layout": "stacked",
  "x_axis": "Time(s)",
  "y_axes": ["Temp(K)", "R1(Ohm)", "R2(Ohm)"],
  "x_scale": "linear",
  "y_scale": "log",
  "zoom": {
    "x_range": [0.0, 3600.0],
    "overlay_y_range": null,
    "stacked_y_ranges": {
      "Temp(K)": [1.8, 4.2],
      "R1(Ohm)": [100.0, 900.0]
    }
  }
}
```

字段语义：

| 字段 | 说明 |
|---|---|
| `format` | 固定标识，避免把其他软件的 PLT 当作本格式 |
| `version` | 当前为 2；版本 1 兼容读取，其他版本不会静默套用 |
| `data_file` | 记录原 DAT 文件名，移动 DAT/PLT 文件对时不作为硬拒绝条件 |
| `layout` | `overlay` 或 `stacked` |
| `x_axis` | 数值列名；`null` 表示 Row Number |
| `y_axes` | 按绘制顺序排列的一个或多个数值列名 |
| `x_scale` | `linear` 或以 10 为底的 `log`；版本 1 缺省时为 `linear` |
| `y_scale` | `linear` 或以 10 为底的 `log`；统一应用于已选 Y，版本 1 缺省时为 `linear` |
| `x_range` | 所有曲线/子图共用的人工 X 视野；`null` 表示自动范围 |
| `overlay_y_range` | Overlay 共用的人工 Y 视野 |
| `stacked_y_ranges` | Stacked 中各 Y 子图独立的人工 Y 视野 |

更改布局、轴选择、坐标尺度或框选范围后会自动保存；写入先生成临时文件再替换目标，降低中途失败留下半个 JSON 的风险。浏览器优先读取 `sample.plt`，也兼容读取 `sample.dat.plt`。Log10 的人工范围必须严格大于零。PLT 无效、版本未知、对数范围非法或引用不存在的列时，不改变 DAT 内容，也不套用部分设置。
