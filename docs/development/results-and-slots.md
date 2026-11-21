# 让每个通道各写一行（多通道与结果格式）

如果一台设备要依次测 4 个样品通道，不需要自己在 `measure()` 里写循环——告诉主程序 `slots = 4`，主程序就会按顺序调用 `measure(1, api)`、`measure(2, api)`、`measure(3, api)`、`measure(4, api)`，并且每个通道自动占 DAT 里的独立一行。

`slot` 本质上是多模块协同对齐用的**逻辑通道号**（通常就是 1–4 通道，也可以代表样品位或开关组合）。

## 1. 声明槽位并返回当前通道数据

```python
class Module:
    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "StatusCode": "",
    }
    slots = 4  # 等价于 (1, 2, 3, 4)，也可以写成动态 property 只返回已勾选通道

    def measure(self, channel, api):
        resistance = self.read_channel(channel)
        return {
            f"R{channel}": resistance,
            "StatusCode": 0,
        }
```

### 没测的列直接不返回（留空）

比如测量通道 2 时，字典里只写 `R2` 和 `StatusCode` 即可：

```python
return {"R2": resistance, "StatusCode": 0}
```

**不要**把其他通道填成 `0`、`None`、`"N/A"` 或 `"Error"`。省略未测列后，DAT 中对应单元格会干净地留白，画图和后处理最省心。

## 2. 多个模块同时启用时怎么对齐？

主程序在 Run 开始时收集所有启用模块声明的 `slots` 并集，逐个槽位并行调用参与该槽位的模块：

| DAT 行 | 四通道模块（`slots = (1, 2, 3, 4)`） | 独立单通道模块（未声明 `slots`） |
| --- | --- | --- |
| 槽位 1 | 测通道 1 | 跟随测量一次 |
| 槽位 2 | 测通道 2 | 跟随测量一次 |
| 槽位 3 | 测通道 3 | 跟随测量一次 |
| 槽位 4 | 测通道 4 | 跟随测量一次 |

- 两个四通道模块同时启用时，通道 1 对齐通道 1 写进同一行，以此类推；如果其中一个模块关闭了通道 3，它在第 3 行的列就自动留空。
- 所有启用模块都未声明 `slots` 时，一次 `Measure` 就只执行槽位 1、写一行。

## 3. 数据状态只写整数，告警走 `api.warn()`

不要把 `"Overrange"` 或 `"Error"` 之类的文字写进数据列。用一列整数（如 `StatusCode`）记录本行状态，具体编码由模块自己约定，常见写法如下：

| 状态码 | 常见含义 |
| --- | --- |
| `0` | 正常 |
| `1` | 超量程 |
| `2` | 触发仪表保护限制（compliance） |
| `3` 及以上 | 其他数据无效情况（由模块 README 定义） |

遇到单点数据不可用（例如某个通道超量程，但仪器通信正常）时，**省略无效的测量列**，只返回非零状态码并用 `api.warn()` 发一个可继续运行的告警：

```python
api.warn("OVER_RANGE", "R2 超量程", "R2")
return {"StatusCode": 1}
```

如果发生通信中断、身份不符或输出状态无法确认等严重故障，则直接抛出 `ModuleError` 停机，不要只写状态码硬撑着继续测。

## 4. 需要保存原始采样点（`rawdata`）时

如果你的测量需要把每次平均前的原始电压序列也存下来，让 `measure()` 返回一个二元组 `(row, raw_values)` 即可：

```python
row = {
    f"R{channel}": mean_resistance,
    "ResistanceStdDev": stddev,
    "SampleCount": len(raw_values),
    "StatusCode": 0,
}
return row, raw_values
```

主程序会自动校验全为有限数字，并按行顺序把它们保存到运行目录的 `rawdata/` 独立文件中，不会污染主 DAT 的列结构。模块不需要自己读写磁盘文件。

## 5. 在主窗口左侧监视卡展示实时数值

只要在类上加一行 `display_columns`（从 `columns` 里任选最多 8 列）：

```python
display_columns = ("R1", "R2", "R3", "R4")
```

主窗口左侧的模块卡片就会在模块窗口隐藏时实时显示最近结果（自动缩成 `1 pΩ`、`2 mV`、`5 kΩ` 等紧凑格式）。

- 每轮新 `Measure` 开始前会自动清空上一轮缓存，空值或报警行显示 `—`，不会让人误看旧数；
- 卡片只展示已经校验返回的测量结果，绝不会为了刷新界面额外访问硬件。
