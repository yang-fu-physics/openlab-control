# 多通道、测量结果与状态

一次 `Measure` 不等于固定一行。核心先收集所有 Enabled 模块声明的逻辑 slot，再按 slot
逐行调度。

## Slot 合并规则

假设同时启用三个模块：

| 模块 | 声明 | 参与方式 |
| --- | --- | --- |
| 四通道电桥 | `slots = 4` | slot 1–4 |
| 只启用奇数通道的模块 | `slots = (1, 3)` | 只参与 slot 1、3 |
| 独立 2400 | 不声明 `slots` | 跟随每个现有 slot 调用一次 |

逻辑 slot 的并集是 1、2、3、4，因此一次 `Measure` 写四行：

| 行 | 电桥 | 奇数通道模块 | 独立 2400 |
| --- | --- | --- | --- |
| slot 1 | 测量 | 测量 | 测量一次 |
| slot 2 | 测量 | 空列 | 测量一次 |
| slot 3 | 测量 | 测量 | 测量一次 |
| slot 4 | 测量 | 空列 | 测量一次 |

同一 slot 的不同模块并行等待；同一个模块内部仍严格串行。若所有模块都不声明 `slots`，
一次 Measure 只有 slot 1。

## 稀疏行，而不是填文字

每个通道只返回自己的列：

```python
def measure(self, slot, api):
    resistance = self.read_channel(slot)
    return {
        f"R{slot}": resistance,
        "StatusCode": 0,
    }
```

不要给未测量通道返回 `0`、`None`、`"N/A"` 或 `"Error"`。直接省略键，中央 DAT 写入器
会保持该列为空。

## 数据异常和系统异常不同

状态码由每个模块定义，不存在强行统一的全局含义。常见约定可以是：

| 数值 | 某个模块中的含义示例 |
| --- | --- |
| `0` | 正常 |
| `1` | 超量程 |
| `2` | 超 compliance |
| `3+` | 作者定义的其他数据状态 |

如果某次电阻无效：

```python
return {
    "ExcitationCurrent": current,
    "StatusCode": STATUS_OVER_RANGE,
}
```

电阻列被省略，同时用 `api.warn(...)` 报告可恢复数据问题。SEQ 继续，Warning 按
`source/code/context` 去重。

如果通讯、输出状态、联锁或安全状态无法确认，应抛 `ModuleError` 或普通异常，使整个 SEQ
进入 Error。不要用一个普通状态码掩盖系统故障。

## 原始样本

需要保存仪表原始读数时返回二元组：

```python
row = {
    f"R{slot}": mean_resistance,
    "ResistanceStdDev": stddev,
    "SampleCount": len(raw_values),
    "StatusCode": 0,
}
return row, raw_values
```

核心把每个模块的有限数值序列写到独立无表头 rawdata sidecar，每一行与正式 DAT 行对应。
模块不能自行写主 DAT，也没有 `emit_row`。

边界要求：

- rawdata 只能是有限数字；
- 单次最多 32,768 个值；
- Mapping 只能包含 `columns` 已声明的键；
- NaN、Infinity、复杂对象和过大 IPC 消息都会被拒绝。

## 温度和磁场快照

核心在一条正式测量行中只取一份温场快照。模块需要在切换前后读取实际环境时，可在自己的
测量流程里调用 `api.devices()` 两次并自行求平均；不要修改核心 DAT 的系统列。
