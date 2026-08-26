# 按需添加设置和状态窗口

先让模块在没有自定义窗口时完成测量。只有确实需要设置电流、量程、等待时间等参数时，才
增加 `frontend.py`。

这个文件只负责显示和收集输入。连接仪表、发送命令和检查安全状态仍放在 `backend.py`。

## 一个可直接作为 frontend.py 使用的窗口

下面的例子提供一个电流输入、一个连接状态和一个测试按钮：

```python
from PySide6.QtWidgets import (
    QComboBox,
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

        settings_layout = QFormLayout(self)
        self.current_input = QDoubleSpinBox()
        self.current_input.setDecimals(6)
        self.current_input.setRange(0.0, 0.01)
        self.current_input.setSuffix(" A")
        settings_layout.addRow("Current", self.current_input)

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
        self.current_input.setValue(
            float(settings.get("current_a", 0.001))
        )

    def dump(self):
        return {"current_a": self.current_input.value()}

    def show_status(self, status):
        self.connection_label.setText(
            str(status.get("Connection", "—"))
        )
```

类名必须是 `Frontend`。主程序会把这个 QWidget 放入 Settings 页，并把
`status_widget` 放入 Status 页。

- `load` 只把保存的数值显示出来，不会发送给仪表；
- `dump` 在用户点击 **Apply Settings** 时收集当前数值；
- `show_status` 显示后台最近报告的状态；
- `api.action` 只向后台发一个请求，不在界面线程中访问仪表。

“加载设置”和“应用设置”故意分开。打开模块或载入 SEQ 时，不会因为旧设置而自动改变
仪表。

## 从未分配 VISA 清单选择测量仪表

设置窗口不要自己扫描 VISA。Instrument Scanner 已把未分配给 System Instrument 的地址写入
`configs/visa.resources.toml`，前端只需把稳定 ID 放进下拉框：

```python
self.resource_input = QComboBox()
for resource_id, info in api.resources().items():
    identity = info.get("identity") or info["address"]
    self.resource_input.addItem(
        f"{resource_id} — {identity}",
        resource_id,
    )
settings_layout.addRow("Instrument", self.resource_input)
```

`dump()` 保存 `resource_id`，不要保存原始 GPIB/USB 地址。后台在 `open` 或 `configure`
中调用同名 `api.resources()` 取得本次不可变资源快照，再解析
`api.resource_address(resource_id)`。前端和后台都只能得到深拷贝，不能修改核心配置。

## 在 backend.py 应用设置

用户点击 **Apply Settings** 后，`dump()` 返回的 Mapping 会传给后台 `configure`。在
`backend.py` 的 `Module` 类中添加下面的方法；后台必须重新检查范围、发送命令并读回确认，
只有成功后才更新实际使用值：

```python
from labcontrol.module_api import ModuleError


def configure(self, settings, api):
    current_a = float(settings.get("current_a", 0.001))
    if not 0 < current_a <= 0.01:
        raise ModuleError(
            "Current must be within (0, 0.01] A",
            "CURRENT_OUT_OF_RANGE",
        )

    self.instrument.set_current(current_a)
    actual_a = self.instrument.read_current()
    if abs(actual_a - current_a) > max(1e-12, current_a * 1e-6):
        raise ModuleError(
            "Instrument current readback does not match",
            "CURRENT_READBACK_MISMATCH",
        )

    self.current_a = current_a
    status = {"Connection": "Connected", "Current (A)": actual_a}
    api.status(status)
    return status
```

`self.instrument` 应由 Enable 阶段的 `open()` 创建。上例中的 `set_current` 和
`read_current` 是该模块自己的仪表文件方法；不要从界面代码直接调用它们。

## 在 backend.py 处理按钮请求

上面的按钮会让后台收到 `event == "action"`，动作名和可选参数放在 `data` 中。下面的方法
同样写在 `backend.py` 的 `Module` 类中：

```python
def on_event(self, event, data, api):
    if event == "action" and data.get("name") == "test_connection":
        identity = self.instrument.identify()
        status = {"Connection": "Connected", "Identity": identity}
        api.status(status)
        return status
    return {}
```

后台也可以在连接或测量时主动更新状态：

```python
api.status({"Connection": "Connected", "State": "Ready"})
```

状态文字只用于显示。不能因为标签写着 “Connected” 就跳过真正的仪表读回。

## 主程序已经准备好的功能

不需要自己重复制作：

- Settings 和 Status 两个页面；
- **Apply Settings** 按钮；
- 未应用设置的提示；
- SEQ 运行时锁定设置；
- 防止用户直接关闭模块窗口；
- 自动选择不出现横向滚动条的初始宽度；
- 下拉列表未展开时，阻止滚轮误改数值。
- 继承主程序在 **View → Appearance** 中选择的整体和文字大小；
- 按模块 ID 记住独立窗口尺寸与位置。

用户可能把整体大小设为 75%–200%，并把文字额外设为 70%–150%。请使用 Qt layout、
`sizeHint` 和可滚动设置页，不要使用绝对坐标、固定字号或仅在一种 DPI 下刚好放得下的
固定高度。核心会处理默认窗口宽度和已保存几何，模块不需要读取个人外观配置。

## 窗口代码不要做这些事

- 不要打开 VISA、串口或厂商软件连接；
- 不要自行枚举全部 VISA 地址；只显示 `api.resources()`；
- 不要创建线程访问仪表；
- 不要直接写配置或 DAT 文件；
- 不要控制 SEQ 或其他仪表；
- 不要在 `load()` 中自动应用设置。

??? example "展开教学模块的完整 frontend.py"

    ```python
    --8<-- "modules/tutorial_resistance/frontend.py"
    ```
