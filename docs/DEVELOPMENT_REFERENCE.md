# 完整开发规范

本文汇总 **Measurement Module** 与 **System Instrument** 的全部接口契约，适合在开发过程中作为速查手册；初学者建议先从 [写第一个测量模块](development/first-module.md) 或 [写第一个 System Instrument](development/first-system-instrument.md) 入手。

---

## 一、Measurement Module 契约

### 目录与清单

最小结构（目录名即模块 ID，入口固定为 `backend.py` 内的 `Module` 类）：

```text
modules/my_meter/
├─ module.toml     # 仅需 name 和 version
├─ backend.py      # 后台逻辑（无需继承基类）
└─ frontend.py     # 可选：自定义 PySide6 QWidget 界面
```

```toml
name = "My Meter"
version = "0.1.0"
```

### 必需与可选后端成员

| 成员 | 必需 | 契约说明 |
| --- | --- | --- |
| `columns` | **是** | 有序 `{列名: 单位}` 字典，固定为本模块在 DAT 中允许输出的列 |
| `open(api)` | **是** | Enable 时调用；建立安全初始状态，不接收旧保存设置 |
| `measure(slot, api)` | **是** | 返回一行 `Mapping` 或 `(row_mapping, raw_values_seq)`；未测列省略 |
| `close(api)` | **是** | Disable 或退出时调用；必须幂等，确认安全状态并释放全部连接 |
| `display_columns` | 否 | 最多 8 个已有列名，用于主窗口左侧实时监视卡（不额外访问仪表） |
| `slots` | 否 | 正整数（如 `4` → `(1,2,3,4)`）、正整数序列或动态 `property` |
| `configure(settings, api)` | 否 | 用户明确点击 **Apply Settings** 时调用；校验边界、写仪表并回读 |
| `on_event(event, data, api)` | 否 | 统一处理 `run_start` (`{}`)、`run_end` (`{"reason": "completed"|"stopped"|"error"}`)、`status`、`action` (`{"name": str, "payload": dict}`) |
| `sequence_commands` | 否 | 声明可注册到右侧指令栏的模块自定义指令列表 |
| `execute_sequence_command(command_id, parameters, api)` | 有指令时**是** | 执行普通动作或逐点扫描动作，仅返回状态字典或 `None`（不写 DAT） |

> 后端生命周期方法既可以是普通同步函数，也可以返回 awaitable。只要模块会打开电流/电压输出，就必须处理 `on_event("run_end", ...)`，默认在三种结束原因下关闭输出；若提供跨 SEQ 保持偏置选项，必须默认关闭、保持前回读确认、且 Disable / Apply / 异常时仍强制关闭。

### `sequence_commands` 声明格式

```python
sequence_commands = [
    {
        "id": "set_current",          # 稳定标识：[a-z][a-z0-9_]*
        "label": "Set Current",
        "kind": "command",            # 默认 command；扫描用 scan
        "fields": [
            {"name": "current", "label": "Current", "type": "text", "default": "1 mA"},
            {"name": "output", "label": "Enable output", "type": "bool", "default": True},
        ],
    },
    {
        "id": "scan_current",
        "label": "Scan Current",
        "kind": "scan",
        "points_field": "points",
        "point_parameter": "current",
        "fields": [
            {"name": "points", "label": "Current points", "type": "list", "default": ["1 mA", "2 mA"]},
            {"name": "settle_seconds", "label": "Settle time", "type": "float", "default": 0.0, "minimum": 0.0, "unit": "s"},
        ],
    },
]
```

- 字段类型仅支持：`text`、`int`、`float`、`choice`（需配 `choices`）、`bool`、`list`（最多 100,000 项 JSON 标量）。
- 数值字段可配 `minimum`、`maximum`、`unit`、`decimals`；若需自定义编辑弹窗可设 `custom_editor = True` 并在前端实现 `edit_sequence_command(command_id, parameters)`。
- 扫描指令（`kind = "scan"`）还可声明 `scan_modes`（如 `["Linear", "Log", "List"]`）、`point_unit`（如 `"A"`、`"V"`）、`point_decimals`（生成点按基础单位量化的小数位）与 `point_limits`（如 `{"setting": "max_current", "symmetric": True, "minimum": ..., "maximum": ...}`，在通用参数窗口中自动同步模块 Settings 上限；非扫描命令也可通过 `point_limits.field` 绑定单个文本字段）；既有仅含 `points` 的旧 `.seq` 自动按 `List` 模式兼容。
- 禁止提供“任意 SCPI 透传”指令。

### `ModuleAPI` 常用方法

- `api.sleep(seconds)`：Pause 冻结计时、Stop 可打断；
- `api.checkpoint()`：立即检查 Pause / Stop；
- `api.instruments(timeout=5.0)`：请求一次测量专用即时系统温场快照（并发请求自动合并）；
- `api.resources()` / `api.resource_address(resource_id)`：只读获取扫描器确认的 Measurement VISA 资源；
- `api.warn(code, message, key="")`：触发去重 Warning（传 `message=None` 解除）；
- `api.status(mapping)`：向模块 Status 页推送状态字典；
- `api.timeout`：本次调用的核心总时限（秒）；
- 异常分级：数据异常写整数状态码并 `api.warn()` 或抛 `ModuleWarning`（SEQ 继续）；通信、协议或安全状态不明抛 `ModuleError`（SEQ 中止）。

### 可选前端 `frontend.py`

```python
from PySide6.QtWidgets import QWidget


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def load(self, settings): ...          # 填回界面控件；不得连仪表或自动 Apply
    def dump(self): return {}              # 收集当前控件设置
    # 可选：self.status_widget、show_status(mapping)、api.action(name, payload)、api.refresh()
```

### 共享 VISA 会话

模块应直接使用框架提供的 `labcontrol.measurement.visa.VisaSession`，只需传入终止符和串口参数，关闭时只释放本仪表句柄而不关闭进程共享的 VISA ResourceManager。设置中表示仪表的键建议使用 `resource`、`resource_*` 或 `*_resource`，值存稳定资源 ID，便于核心在 Apply 时做跨模块地址冲突保护。

---

## 二、System Instrument 契约

目录位于 `system_instruments/<id>/`，包含 `instrument.toml`（API v4）与继承 `labcontrol.instruments.base.SystemInstrument` 的同步后端类：

```python
class MyController(SystemInstrument):
    def open(self): ...
    def read_status(self):
        return {
            "value": 4.2,
            "target": 4.0,
            "rate": 1.0,
            "moving": False,
            "ready": True,
            "auxiliary": {"temp_a": 25.0},
        }
    def read_measurement(self):            # 可选；默认调用 read_status()
        return {"value": 4.2}
    def set_target(self, value, rate_per_minute, mode="Settle", *, control): ...
    def hold(self, *, control): ...
    def execute_sequence_command(self, command_id): ...
    def event_responses(self): return ()   # 可选：返回 EventResponseSpec 元组
    def close(self): ...
```

关键约定：

- **多控制回路**：仅 1 个已启用控制端点时可用顶层 `target/rate/moving/ready`；启用多个不同端点时，必须返回 `controls[control_id]` 分别报告这四项；
- **读数与面板**：所有读数名称、单位、小数位只在 `instrument.toml` 声明一次；同一物理仪表的多个面板共享一个子进程与连接；
- **安全与重连**：`set_target()` 须再次检查对应端点的现场上下限；`hold()` 必须用同一次操作里的现读新鲜值；写超时绝不自动重发；不可恢复的安全问题抛 `SafetyViolation` 立即停机；
- **事件联动**：只能通过 `EventResponseSpec(code=..., context=..., action="zero")` 声明纯数据联动，不可直接持有其他仪表对象。

---

## 三、依赖与测试底线

- `PySide6`、`QtAwesome`、`packaging`、`PyVISA` 和 `typing_extensions` 为框架共享依赖，两类扩展均不得重复安装或另建独立环境。
- 真实硬件驱动在接真机前必须通过：身份不符、量程与限值边界、命令顺序、回读不一致、写超时不重放、异常清理、重复关闭以及 Pause/Stop 测试；自动化测试不能替代硬件联锁与人工急停。
