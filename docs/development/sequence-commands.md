# 给模块增加自己的 SEQ 指令

除了通用的 `Measure`，有些测量模块还需要在实验序列里主动执行动作（例如 `Set Current`、`Auto Range`）或按列表扫描参数（例如 `Scan Current`、`Scan Frequency`）。

只要在 `Module` 类上声明 `sequence_commands` 并实现 `execute_sequence_command`，模块 Enable 后就会自动在右侧 **Sequence Command Bar** 出现专属命令组；Disable 后立即消失。不需要修改主程序任何代码。

> 如果 `Measure` 已经能满足全部需求，可以完全跳过本章。

## 1. 声明一个单步命令（`kind = "command"`）

```python
class Module:
    sequence_commands = [
        {
            "id": "set_current",       # 写入 .seq 的稳定 ID，发布后不要随意改名
            "label": "Set Current",    # 在右侧命令栏显示的名称
            "kind": "command",
            "fields": [
                {
                    "name": "current",
                    "label": "Current",
                    "type": "text",    # 极小电流推荐用文本输入并由后端解析 SI 前缀
                    "default": "1 mA",
                }
            ],
        }
    ]
```

支持的字段类型有 `text`、`int`、`float`、`choice`、`bool`、`list`（完整属性见 [完整开发规范](../DEVELOPMENT_REFERENCE.md)）。

> **小数位约定**：普通 `float` 可用 `decimals` 指定小数位（默认 9 位）；单位为 `unit = "s"` 的时间字段（不含 `filter`）和浮点 NPLC 字段统一为 1 位小数，硬件要求整数时请声明为 `int`。对跨多个数量级的电流/电压（如 `10 pA` 到 `10 mA`），推荐用 `text` 类型并在后端解析 SI 单位。

## 2. 声明一个可嵌套的扫描命令（`kind = "scan"`）

```python
{
    "id": "scan_current",
    "label": "Scan Current",
    "kind": "scan",
    "points_field": "points",       # 保存扫描点列表的字段名
    "point_parameter": "current",   # 每次调用时把当前点放进这个参数键里
    "fields": [
        {
            "name": "points",
            "label": "Current points",
            "type": "list",
            "default": ["100 uA", "200 uA", "500 uA"],
        }
    ],
}
```

生成的 SEQ 就像温场扫描一样支持任意嵌套：

```text
T Module Scan "tutorial_resistance" "scan_current" {"points":["100 uA","200 uA"]}
T     Measure
T End Scan
```

运行时，核心会按列表原顺序逐点调用 `execute_sequence_command()`，该点执行成功后再运行缩进在它下面的所有子指令。

## 3. 在 `backend.py` 中执行指令

```python
from labcontrol.module_api import ModuleError


def execute_sequence_command(self, command_id, parameters, api):
    if command_id != "set_current":
        raise ModuleError(
            f"Unknown command: {command_id}",
            "UNKNOWN_SEQUENCE_COMMAND",
            command_id,
        )

    # 弹窗校验只是提前提示；真正写仪表前必须在后端重新校验范围与状态
    current = parse_current(parameters["current"])
    validate_hardware_range(current)
    self.instrument.set_current(current)
    self.instrument.verify_current(current)
    return {"Current": current}
```

## 注意事项

- **不自动写 DAT**：自定义命令只负责设置或扫描仪表，不产生数据行；需要记录数据时，在命令后面或扫描块内放一条 `Measure`。
- **不改 Settings 页**：执行序列指令只影响当次运行中的仪表状态，不会覆盖 Settings 页保存的默认参数。
- **异常处理**：
  - 某一点数据不佳但可继续：抛 `ModuleWarning`（普通命令跳到下一条，扫描命令跳过当前点的子指令并继续下一点）；
  - 通信失败、超范围或输出状态不明：抛 `ModuleError` 立即中止整个 SEQ。
- **切勿提供“任意 SCPI 透传”指令**：任意指令输入框会绕过范围检查与安全状态机。

完整可运行示例请参考 `modules/tutorial_resistance/backend.py`。
