# 给模块增加自己的 SEQ 指令

有些模块除了 `Measure`，还需要“设置电流”或“扫描电流”。模块可以在 Enable 后把这些
指令加入右侧的 Sequence Command Bar。

如果 `Measure` 已经能完成全部工作，就不需要本章。

## 1. 告诉程序要显示什么

在 `Module` 类中加入：

```python
sequence_commands = [
    {
        "id": "set_current",
        "label": "Set Current",
        "kind": "command",
        "fields": [
            {
                "name": "current",
                "label": "Current",
                "type": "text",
                "default": "1 mA",
            }
        ],
    }
]
```

模块成功 Enable 后，程序会出现一个以模块名称命名的指令组。Disable 后，这组指令会消失。

常用输入类型只有：文字、整数、小数、下拉选项、开关和列表。完整写法可在需要时查看
[完整开发规范](../DEVELOPMENT_REFERENCE.md)。

## 2. 收到指令后执行动作

```python
def execute_sequence_command(self, command_id, parameters, api):
    if command_id != "set_current":
        raise ModuleError(
            f"Unknown command: {command_id}",
            "UNKNOWN_SEQUENCE_COMMAND",
            command_id,
        )

    current = parse_current(parameters["current"])
    validate_hardware_range(current)
    self.instrument.set_current(current)
    self.instrument.verify_current(current)
    return {"Current": current}
```

参数窗口会做简单检查，但发送仪表命令前仍要再次检查范围。写入后应尽可能读回确认。

这类指令本身不写 DAT。需要记录一次数据时，在它后面再放一条 `Measure`。

## 3. 需要扫描时再增加 Scan

```python
{
    "id": "scan_current",
    "label": "Scan Current",
    "kind": "scan",
    "points_field": "points",
    "point_parameter": "current",
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

程序会逐个把列表中的电流交给 `execute_sequence_command`，然后运行这个 Scan 里面的指令。
因此可以把 `Measure` 放进 Scan 中。

生成的 SEQ 大致如下：

```text
T Module Scan "tutorial_resistance" "scan_current" {"points":["100 uA","200 uA"]}
T     Measure
T End Scan
```

## 出错时怎样处理

- 某一点数据不好但可以继续：报告 Warning；
- 通讯失败、范围不安全或输出状态不明：报告 Error，停止 SEQ；
- 保存的 SEQ 缺少对应模块时：可以打开和编辑，但不能开始运行。

不要提供“任意仪表命令”输入框。它很容易绕过范围检查和安全步骤。

完整可运行例子在
`templates/measurement-modules-repository/modules/tutorial_resistance/backend.py`。
