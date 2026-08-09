# 写第一个测量模块

这一章做一个不连接真实仪表的四通道电阻模块。完整例子已经放在
`plugin_templates/measurement-modules-repository/modules/tutorial_resistance/`，可以直接复制。

## 1. 创建两个文件

```text
modules/tutorial_resistance/
├─ module.toml
└─ backend.py
```

`module.toml` 告诉程序模块的名称和版本：

```toml
name = "Tutorial Resistance"
version = "0.1.0"
```

文件夹名是模块在程序内部使用的名字。建议只用小写字母、数字和下划线。

## 2. 写启动、测量和关闭

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

这里真正需要记住的只有三件事：

- `open`：点击 Enable 后运行。以后把“打开仪表连接”写在这里。
- `measure`：每次测一个通道并返回一行数据。
- `close`：Disable 或退出时运行。以后把“关闭输出、断开连接”写在这里。

`columns` 是 DAT 中允许出现的列，`slots = 4` 表示有四个通道。

!!! warning "不要过早连接仪表"

    不要在文件刚被读取时连接仪表，也不要在 `__init__` 中连接。必须等到 `open`，这样模块
    保持 Disabled 时不会碰真实仪表。

## 3. 只在需要时使用这些帮助功能

```python
api.sleep(0.5)                              # 等待 0.5 秒，可被 Pause/Stop 控制
api.checkpoint()                            # 检查是否 Pause 或 Stop
api.warn("OVER_RANGE", "R1 超量程", "R1") # 报告一次可继续运行的问题
api.status({"State": "Measuring"})        # 更新模块状态窗口
```

第一次练习不必把它们全部用上。先让 `measure` 正常返回数据即可。

## 4. 复制并运行完整例子

```powershell
Copy-Item -Recurse `
  .\plugin_templates\measurement-modules-repository\modules\tutorial_resistance `
  .\modules\tutorial_resistance
```

重启程序后：

1. 打开 Modules，Enable **Tutorial Resistance**。
2. 打开 Settings，检查数值后点击 **Apply Settings**。
3. 新建一条包含 `Measure` 的 SEQ。
4. 运行后确认写出四行；R1、R2、R3、R4 分别只出现在自己的行。

完整例子多了一些后面才会用到的功能，例如设置窗口、原始数据和模块自己的 SEQ 指令。
不理解这些代码也不会影响本章。

??? example "展开完整 backend.py"

    ```python
    --8<-- "plugin_templates/measurement-modules-repository/modules/tutorial_resistance/backend.py"
    ```

下一步阅读 [多通道数据](results-and-slots.md)。
