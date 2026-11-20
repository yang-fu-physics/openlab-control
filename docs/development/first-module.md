# 写第一个测量模块

这一节做一个不连接真机的四通道电阻模块。你可以先运行程序自带的完整教学版，再从零写出一个只有两个文件的最小版本。

> **接入真实仪表小贴士**：动笔写 SCPI 前，建议先查 [PyMeasure 驱动](https://pymeasure.readthedocs.io/en/latest/api/instruments/index.html)、[QCoDeS 官方驱动](https://microsoft.github.io/Qcodes/drivers_api/index.html) 或 [QCoDeS 社区驱动](https://github.com/QCoDeS/Qcodes_contrib_drivers)，有匹配的底层命令和解析逻辑可以优先对照复用，再拿仪表手册逐条核对。

!!! info "开始之前"

    请先按 [Windows 发布包](../getting-started/windows.md) 或 [源码环境](../getting-started/source.md) 启动软件，
    并至少跑通一次 [第一条 SEQ](../getting-started/first-sequence.md)。

## 路线 A：先跑一遍自带的完整教学模块

软件的 `modules/tutorial_resistance/` 已经放好了一个完整教学模块：

1. 启动 OpenLab Control，打开工具栏 **Modules**，勾选 **Tutorial Resistance**。
2. 在弹出的窗口中核对参数，点击 **Apply Settings**。
3. 运行一条包含 `Measure` 的 SEQ。
4. 用 [Data Browser](../guides/data-browser.md) 打开本次 `runs/<时间>_<SEQ>/experiment.dat`，观察每个 `Measure` 写出的四行数据（`R1`–`R4` 各自只出现在自己的行）。

这个模块还演示了自定义窗口、`rawdata` 和模块 SEQ 指令，第一次跑通时不必全部读懂。

??? example "展开查看 tutorial_resistance/backend.py 完整源码"

    ```python
    --8<-- "modules/tutorial_resistance/backend.py"
    ```

## 路线 B：从两个文件写出最小模块

在 `modules/` 下新建一个独立目录（避免覆盖教学模块），目录名即模块 ID（小写字母、数字和下划线）：

```text
modules/tutorial_minimal/
├─ module.toml
└─ backend.py
```

**1. 编写 `module.toml`**

```toml
name = "Tutorial Minimal"
version = "0.1.0"
```

**2. 编写 `backend.py`**

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
    display_columns = ("R1", "R2", "R3", "R4")
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

**3. 启用并试跑**

重启软件（或在所有模块关闭时点 **Modules → Refresh**），勾选 **Tutorial Minimal**。因为它没有自定义设置窗口，不需要点 Apply，直接运行含 `Measure` 的 SEQ 就能写出四通道数据。

### 核心概念速记

- `columns`：声明要写入 DAT 的列名与单位（顺序固定）。
- `display_columns`（可选）：从中挑最多 8 列显示在主窗口左侧的模块监视卡里。窗口关闭隐藏后，卡片仍会实时更新已返回的结果（不会为了刷新界面额外读仪表）。
- `slots = 4`：告诉主程序每次 `Measure` 按 1、2、3、4 依次调用 `measure(channel, api)` 并各写一行。
- `open` / `measure` / `close`：注意**不要在模块顶层 import 或 `__init__` 里连接真实仪表**，连接必须延迟到 `open()` 或 `configure()`，这样未勾选时绝不会碰硬件。

## 常用的 `api` 辅助方法

```python
api.sleep(0.5)                              # 可被 Pause 冻结、可被 Stop 打断的等待
api.checkpoint()                            # 在长循环或两次 I/O 间立即响应 Pause/Stop
api.warn("OVER_RANGE", "R1 超量程", "R1")   # 报告可继续运行的 Warning（传 None 解除）
api.status({"State": "Measuring"})          # 更新模块 Status 页显示
resources = api.resources()                 # 读取扫描器确认过的 Measurement VISA 清单
address = api.resource_address("k2400_1")   # 将稳定资源 ID 解析为当前物理地址
```

真实模块请在设置里保存稳定的资源 ID，不要自己写一套全盘 VISA 扫描；先用 [Instrument Scanner](../guides/instrument-scanner.md) 统一登记仪表即可。

下一步阅读：[让每个通道各写一行](results-and-slots.md)。
