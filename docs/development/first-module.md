# 写第一个测量模块

这一章做一个不连接真实仪表的四通道电阻模块。你可以先复制完整版确认流程，也可以从
两个文件开始学习。两条路线使用不同的文件夹，不会互相覆盖。

!!! info "开始前"

    先完成 [Windows 发布包](../getting-started/windows.md) 或
    [源码环境](../getting-started/source.md)，并至少运行一次
    [第一条 SEQ](../getting-started/first-sequence.md)。下面的 PowerShell 命令都在
    OpenLabControl 根目录执行。

## 路线 A：先运行完整例子

这是最快看到结果的方式。目标目录还不存在时执行：

```powershell
Copy-Item -Recurse `
  .\plugin_templates\measurement-modules-repository\modules\tutorial_resistance `
  .\modules\tutorial_resistance
```

重启程序后：

1. 打开 **Modules**，Enable **Tutorial Resistance**。
2. 打开 Settings，检查数值后点击 **Apply Settings**。
3. 按 [第一条 SEQ](../getting-started/first-sequence.md) 新建含 `Measure` 的 SEQ 并运行。
4. 用 [Data Browser](../guides/data-browser.md) 打开本次
   `runs/<时间>_<SEQ>/experiment.dat`。确认每个 Measure 写出四行，R1、R2、R3、R4
   分别只出现在自己的行。

这个完整版包含设置窗口、原始数据和模块自己的 SEQ 指令。第一次运行不需要先读懂它们。

??? example "展开完整 backend.py"

    ```python
    --8<-- "plugin_templates/measurement-modules-repository/modules/tutorial_resistance/backend.py"
    ```

## 路线 B：从两个文件开始

新建另一个目录，避免覆盖路线 A：

```text
modules/tutorial_minimal/
├─ module.toml
└─ backend.py
```

把下面内容放进 `module.toml`：

```toml
name = "Tutorial Minimal"
version = "0.1.0"
```

文件夹名是模块在程序内部使用的名字。建议只用小写字母、数字和下划线。

把下面内容放进 `backend.py`：

```python
from labcontrol.module_api import ModuleError


class Module:
    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "StatusCode": "",
    }
    slots = 4

    def __init__(self):
        self.ready = False

    def open(self, api):
        self.ready = True
        api.status({"State": "Ready"})

    def measure(self, channel, api):
        if not self.ready:
            raise ModuleError("Module is not ready", "NOT_READY")
        api.checkpoint()
        return {
            f"R{channel}": 100.0 + channel,
            "StatusCode": 0,
        }

    def close(self, api):
        self.ready = False
```

重启并 Enable **Tutorial Minimal**。它没有 `frontend.py`，所以没有自定义设置，也不需要
点击 Apply Settings。直接运行含 `Measure` 的 SEQ 即可。

这里先记住：

- `open`：点击 Enable 后运行；以后在这里打开仪表连接。
- `measure`：每次测一个逻辑通道并返回一行数据。
- `close`：Disable 或退出时运行；再次确认输出关闭，再断开连接。
- `columns`：DAT 中允许出现的列。
- `slots = 4`：一次 `Measure` 依次请求四个逻辑行键。

!!! warning "真实输出必须在 run_end 关闭"

    `close` 不会在每次 SEQ 结束时调用。真实模块若会打开电流、电压或其他输出，必须在
    `on_event("run_end", ...)` 中关闭；正常完成、Stop 和 Error 都走这条收尾。`close`
    仍要再做一次幂等关闭，作为 Disable 和应用退出时的最后保障。

## 只在需要时使用帮助功能

```python
api.sleep(0.5)                              # 等待 0.5 秒，可被 Pause/Stop 控制
api.checkpoint()                            # 检查是否 Pause 或 Stop
api.warn("OVER_RANGE", "R1 超量程", "R1") # 报告一次可继续运行的问题
api.status({"State": "Measuring"})        # 更新模块状态窗口
```

不要在文件刚被读取时连接仪表，也不要在 `__init__` 中连接。必须等到 `open`，这样模块
保持 Disabled 时不会碰真实仪表。

下一步阅读 [多通道数据](results-and-slots.md)。
