# 让每个通道各写一行

`slot` 是多个模块合并结果时共同使用的“逻辑行键”。在本章的四通道例子中，1、2、3、4
恰好对应四个物理通道，所以每个 slot 写一行；其他模块也可以把它用于样品位置、开关组合
或别的逻辑编号，不要求等于仪表面板上的通道号。

## 四通道模块

```python
class Module:
    slots = 4

    def measure(self, channel, api):
        resistance = self.read_channel(channel)
        return {
            f"R{channel}": resistance,
            "StatusCode": 0,
        }
```

一次 `Measure` 会调用四次：通道 1、2、3、4 各一次，并写成四行。模块不用在
`measure` 里面再循环四次。

## 没测量的列保持空白

测量通道 2 时，只返回 R2：

```python
return {
    "R2": resistance,
    "StatusCode": 0,
}
```

不要给 R1、R3、R4 填 `0`、`None`、`"N/A"` 或 `"Error"`。不返回这些列，DAT 会自然
留下空白。

## 同时启用多个模块

假设同时启用一个四通道模块和一台独立 2400：

| DAT 行 | 四通道模块 | 独立 2400 |
| --- | --- | --- |
| 通道 1 | 测通道 1 | 测量一次 |
| 通道 2 | 测通道 2 | 再测量一次 |
| 通道 3 | 测通道 3 | 再测量一次 |
| 通道 4 | 测通道 4 | 再测量一次 |

不声明 `slots` 的模块会跟随每一行测量。两个四通道模块同时启用时，则按通道号对齐：
通道 1 和通道 1 写在同一行，通道 2 和通道 2 写在同一行。

模块也可以返回非连续的 slot 列表。核心按所有 Enabled 模块的 slot 并集逐行调用；若所有
模块都不声明 `slots`，一次 `Measure` 只产生一个逻辑行。

如果某个模块没有启用某一通道，它在那一行的列保持空白，其他模块仍然正常写入。

## 状态只写数字

每个模块可以自己规定数字含义。例如：

| 数值 | 示例含义 |
| --- | --- |
| `0` | 正常 |
| `1` | 超量程 |
| `2` | 触发仪表的保护限制（手册中常写 compliance） |
| `3` 及以上 | 模块自己定义 |

测量值无效时，不返回电阻列，只返回状态：

```python
api.warn("OVER_RANGE", "R2 超量程", "R2")
return {
    "StatusCode": 1,
}
```

这种情况只表示本次数据不可用，SEQ 可以继续。如果仪表断线、输出状态无法确认或安全状态
不明，则应该报 Error 并停止 SEQ，不能只写一个状态码继续运行。

## 需要保存仪表原始读数时

这一步是可选的。返回“正式数据 + 原始数字列表”即可：

```python
row = {
    f"R{channel}": mean_resistance,
    "ResistanceStdDev": stddev,
    "SampleCount": len(raw_values),
    "StatusCode": 0,
}
return row, raw_values
```

主程序会把原始数字写进单独的 rawdata 文件。模块不要自己写 DAT，也不要在原始数据中放
文字、`NaN` 或 `Infinity`。

公共数据列由主程序写入。测量模块只返回自己声明的测量列。

## 在主窗口显示最近结果

如果希望模块窗口最小化后仍能看到几个关键数值，在 `columns` 下面增加一行：

```python
class Module:
    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "StatusCode": "",
    }
    display_columns = ("R1", "R2", "R3", "R4")
    slots = 4
```

`display_columns` 只能填写 `columns` 中已有的名字，最多八个。它是可选的：不写时模块
照常 Enable、测量和写 DAT，只是不在紧凑卡片中显示数值。

- 卡片位于主窗口左侧 `Sequence Status` 下方，点击可恢复模块窗口。
- 声明 `slots` 的模块按逻辑通道保留本轮结果；未声明的模块只显示最近一次返回结果。
- 每次新的 `Measure` 开始前会清空上一轮缓存。空值或报警行显示 `—`，不会沿用旧数字。
- 数值会自动缩短成 `1 pΩ`、`2 mV`、`5 kΩ` 等写法。
- 卡片只接收已经校验过的测量结果，不调用模块方法，也不会增加 VISA/串口访问。

不要把仪表读取函数或自定义格式化函数放进 `display_columns`；这里只写列名即可。
