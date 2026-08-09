# 开发第一个 Measurement Module

这一章先完成一个没有真实仪表的四通道模块。最终代码已经放在
`plugin_templates/measurement-modules-repository/modules/tutorial_resistance/`，可以一边
阅读一边运行。

## 1. 创建最小目录

目录名就是稳定模块 ID。不要把显示名称当作 ID：

```text
modules/tutorial_resistance/
├─ module.toml
└─ backend.py
```

`module.toml` 只需要名称和版本：

```toml
name = "Tutorial Resistance"
version = "0.1.0"
```

发现阶段只读取清单和目录指纹，不导入 `backend.py`，所以扫描模块列表不会执行作者代码。

## 2. 实现三个必需方法

```python
from labcontrol.module_api import ModuleAPI, ModuleError


class Module:
    columns = {"Resistance": "Ohm", "StatusCode": ""}

    def open(self, api: ModuleAPI):
        self.ready = True
        api.status({"State": "Ready"})

    def measure(self, slot: int, api: ModuleAPI):
        if not self.ready:
            raise ModuleError("Module is not ready", "NOT_READY")
        api.checkpoint()
        return {"Resistance": 100.0, "StatusCode": 0}

    def close(self, api: ModuleAPI):
        self.ready = False
```

三个方法都可以是同步函数或返回 awaitable：

- `open(api)`：用户 Enable 后调用。建立连接并进入安全初态，但不会收到保存设置。
- `measure(slot, api)`：一次调用最多返回一行。
- `close(api)`：Disable 和应用退出时调用，必须允许部分初始化和重复调用。

!!! warning "不要在导入和构造阶段连接仪表"

    `backend.py` 被 worker 导入、`Module()` 被构造时都不得发 I/O。真实连接必须推迟到
    `open()`，否则首次信任和生命周期控制会被绕过。

## 3. 声明 DAT 列和通道

四通道模块只需要：

```python
class Module:
    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "StatusCode": "",
    }
    slots = 4
```

核心会依次调用 `measure(1, api)` 到 `measure(4, api)`。模块在每次调用中只测一个通道，
不再自行循环四次。

## 4. 加入 Apply Settings

只有需要设置时才实现 `configure`：

```python
def configure(self, settings, api):
    delay = float(settings["delay_seconds"])
    if not 0 <= delay <= 60:
        raise ModuleError(
            "delay_seconds must be between 0 and 60",
            "INVALID_SETTINGS",
            "delay_seconds",
        )
    self.delay_seconds = delay
    self.applied = True
```

Enable 读取保存值到界面，但不调用 `configure`。只有用户检查后点击 Settings 页的
**Apply Settings**，核心才把 `Frontend.dump()` 的结果传给后端。

## 5. 使用 ModuleAPI

模块通常只需要这些能力：

```python
api.sleep(0.5)                 # Pause 冻结计时，Stop 可打断
api.checkpoint()               # 两次仪表 I/O 之间检查 Pause/Stop
devices = api.devices()        # 最新温度、磁场和 Monitor 快照副本
api.warn("OVER_RANGE", "R1 exceeded range", "R1")
api.warn("OVER_RANGE", None, "R1")  # 解除同一 Warning
api.status({"State": "Measuring"})
operation_limit = api.timeout  # 本次核心操作总时限
```

`ModuleAPI` 不能控制温度、磁场、SEQ 或 DAT，也不能中断已经进入厂商驱动的阻塞调用。

## 6. 运行完整教程模块

把目录复制到活动模块目录：

```powershell
Copy-Item -Recurse `
  .\plugin_templates\measurement-modules-repository\modules\tutorial_resistance `
  .\modules\tutorial_resistance
```

重启程序后：

1. 打开 Modules，Enable **Tutorial Resistance**。
2. 检查 Settings，点击 **Apply Settings**。
3. 创建一条含 `Measure` 的 SEQ。
4. 运行后确认每次 Measure 写四行，R1–R4 分别只出现在自己的行。

??? example "展开完整 backend.py"

    ```python
    --8<-- "plugin_templates/measurement-modules-repository/modules/tutorial_resistance/backend.py"
    ```

接下来阅读 [多通道与测量结果](results-and-slots.md)，理解多个模块如何对齐到同一组行。
