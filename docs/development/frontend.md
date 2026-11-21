# 按需添加设置与状态窗口

简单模块可以不写 `frontend.py`，主程序会自动提供默认空页。当你需要让操作员在界面上配置电流、量程、等待时间，或点击 `Test Connection` 查看仪表状态时，再添加 `frontend.py`。

> **核心分工**：`frontend.py` 只管画界面、读输入、显示状态，**绝不在界面里连仪表或开线程**；一切硬件通信和范围保护都留在 `backend.py`。

## 1. 一个最小可用的 `frontend.py`

类名固定为 `Frontend`（继承自 `QWidget`），主程序会把它放进 **Settings** 页，并把可选的 `self.status_widget` 放进 **Status** 页：

```python
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api

        # Settings 页内容（直接布局在 self 上）
        settings_layout = QFormLayout(self)
        self.current_input = QDoubleSpinBox()
        self.current_input.setDecimals(6)
        self.current_input.setRange(0.0, 0.01)
        self.current_input.setSuffix(" A")
        settings_layout.addRow("Current", self.current_input)

        # 可选的 Status 页内容
        self.status_widget = QWidget()
        status_layout = QVBoxLayout(self.status_widget)
        self.connection_label = QLabel("Not connected")
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(
            lambda: self.api.action("test_connection")
        )
        status_layout.addWidget(self.connection_label)
        status_layout.addWidget(self.test_button)
        status_layout.addStretch(1)

    def load(self, settings):
        # 把保存或随 SEQ 导入的参数填进控件；绝不发给仪表
        self.current_input.setValue(float(settings.get("current_a", 0.001)))

    def dump(self):
        # 返回当前控件的值，用于保存和点击 Apply 时传给后台
        return {"current_a": self.current_input.value()}

    def show_status(self, status):
        # 刷新只读状态显示
        self.connection_label.setText(str(status.get("Connection", "—")))
```

## 2. 让用户从扫描器清单中选择测量仪表

不要在前端自己扫描 VISA。直接调用 `api.resources()` 列出扫描器确认过的 Measurement 资源即可：

```python
from PySide6.QtWidgets import QComboBox

self.resource_input = QComboBox()
for resource_id, info in api.resources().items():
    identity = info.get("identity") or info["address"]
    self.resource_input.addItem(f"{resource_id} — {identity}", resource_id)
settings_layout.addRow("Instrument", self.resource_input)
```

`dump()` 保存稳定的 `resource_id`（建议设置键名使用 `resource`、`resource_*` 或 `*_resource`，便于核心在 Apply 时自动检查跨模块资源占用），后台连接时再用 `api.resource_address(resource_id)` 解析真实地址。

## 3. 在 `backend.py` 接收配置和按钮事件

点击 **Apply Settings** 后，主程序把 `dump()` 的字典传给后台 `configure(settings, api)`；点击自定义按钮调用 `self.api.action("test_connection")` 后，后台通过 `on_event("action", ...)` 收到请求：

```python
from labcontrol.module_api import ModuleError


class Module:
    # ...

    def configure(self, settings, api):
        current_a = float(settings.get("current_a", 0.001))
        if not 0 < current_a <= 0.01:
            raise ModuleError("Current must be within (0, 0.01] A", "CURRENT_OUT_OF_RANGE")

        self.instrument.set_current(current_a)
        actual_a = self.instrument.read_current()
        if abs(actual_a - current_a) > max(1e-12, current_a * 1e-6):
            raise ModuleError("Instrument current readback does not match", "CURRENT_READBACK_MISMATCH")

        self.current_a = current_a
        status = {"Connection": "Connected", "Current (A)": actual_a}
        api.status(status)
        return status

    def on_event(self, event, data, api):
        if event == "action" and data.get("name") == "test_connection":
            identity = self.instrument.identify()
            status = {"Connection": "Connected", "Identity": identity}
            api.status(status)
            return status
        return {}
```

## 4. 主程序已经替你做好了什么？

你不需要在每个模块里重复写下面这些功能，主窗口会自动包办：

- Settings / Status 两个标签页以及底部的 **Apply Settings** 按钮；
- 自动对比未应用改动并在 Run 前提醒；
- SEQ 运行期间自动锁定 Settings 页面；
- 窗口关闭/最小化时安全隐藏并记住每个模块窗口的尺寸位置；
- 阻止未展开的下拉框被鼠标滚轮误改数值；
- 自动继承 **View → Appearance** 的全局缩放与文字倍率。

因此布局时请使用标准的 Qt Layout 和可滚动区域，不要写死像素坐标或固定高度，确保在不同 DPI 和缩放倍率下都不会出现横向滚动条。

??? example "展开查看 tutorial_resistance/frontend.py 完整源码"

    ```python
    --8<-- "modules/tutorial_resistance/frontend.py"
    ```
