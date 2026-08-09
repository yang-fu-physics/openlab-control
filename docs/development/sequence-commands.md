# 给模块增加 SEQ 指令

当模块需要“设置电流”“扫描电流”“切换量程”等不直接写 DAT 的动作时，可以在 Enable 后
向 Sequence Command Bar 动态注册指令。不需要修改核心枚举、解析器或对话框。

## 声明一条普通指令

```python
class Module:
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
                },
                {
                    "name": "settle_seconds",
                    "label": "Settle time",
                    "type": "float",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 60.0,
                    "unit": "s",
                },
            ],
        }
    ]
```

支持的字段只有 `text`、`int`、`float`、`choice`、`bool` 和 `list`。参数窗口只是第一层
输入校验；后端发送仪表命令前仍必须重新解析单位、验证范围和当前状态。

## 声明一个可嵌套 Scan

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
        },
        {
            "name": "settle_seconds",
            "label": "Settle time",
            "type": "float",
            "default": 0.0,
            "minimum": 0.0,
            "maximum": 60.0,
            "unit": "s",
        },
    ],
}
```

核心按原列表逐点调用后端，把当前点放入 `point_parameter` 指定的键；该点成功后才递归
运行 Scan 子树。

## 统一执行入口

```python
def execute_sequence_command(self, command_id, parameters, api):
    if command_id not in {"set_current", "scan_current"}:
        raise ModuleError(
            f"Unknown command: {command_id}",
            "UNKNOWN_SEQUENCE_COMMAND",
            command_id,
        )
    current = parse_current(parameters["current"])
    validate_hardware_range(current)
    instrument.set_current(current)
    instrument.verify_current(current)
    api.sleep(float(parameters.get("settle_seconds", 0.0)))
    return {"Current": current}
```

普通指令和每个 Scan 点都走这个方法。同一模块的指令、生命周期和测量不会并发访问同一
VISA session。

## DAT 和异常语义

- 模块指令返回的是状态 Mapping 或 `None`，本身不写 DAT。
- 要记录测量，必须在普通指令之后或 Scan 子树中显式插入 `Measure`。
- `ModuleWarning` 结束当前普通动作并继续；在 Scan 中会跳过当前点的子树。
- `ModuleError`、通信异常或未处理异常中止整个 SEQ。
- 不要提供“任意 SCPI”文本框，它会绕过可审查参数和安全状态机。

生成的通用 SEQ 文本类似：

```text
T Module Command "tutorial_resistance" "set_current" {"current":"1 mA","settle_seconds":0}
T Module Scan "tutorial_resistance" "scan_current" {"points":["100 uA","200 uA"],"settle_seconds":0}
T     Measure
T End Scan
```

## 动态注册和缺失模块

- 模块成功 Enable 后才出现以模块显示名称为标题的指令组。
- Disable、worker timeout 或 IPC 失效时立即移除。
- SEQ 中缺失的模块 ID 或旧指令 ID 仍可加载、标红和原样保存。
- Run 预检会 fail-closed；不会自动 Enable、安装或静默跳过。

教程模块的完整声明和解析实现位于
`plugin_templates/measurement-modules-repository/modules/tutorial_resistance/backend.py`。
