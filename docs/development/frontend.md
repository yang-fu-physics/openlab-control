# 按需添加设置和状态窗口

先让模块在没有自定义窗口时完成测量。只有确实需要设置电流、量程、等待时间等参数时，才
增加 `frontend.py`。

这个文件只负责窗口。连接仪表和发送命令仍然放在 `backend.py`。

## 最小窗口

```python
from PySide6.QtWidgets import QWidget


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def load(self, settings):
        """把保存的数值显示在窗口中。"""

    def dump(self):
        """收集窗口中的当前数值。"""
        return {}
```

只需要记住：

- `load` 把保存的设置填进窗口，但不会发送给仪表；
- `dump` 在用户点击 **Apply Settings** 时收集设置；
- 类名必须是 `Frontend`。

“加载设置”和“应用设置”故意分开。打开模块或载入 SEQ 时，不会因为旧设置而自动改变
仪表。

## 主程序已经准备好的功能

不需要自己重复制作：

- Settings 和 Status 两个页面；
- **Apply Settings** 按钮；
- 未应用设置的提示；
- SEQ 运行时锁定设置；
- 防止用户直接关闭模块窗口；
- 自动选择不出现横向滚动条的初始宽度；
- 下拉列表未展开时，阻止滚轮误改数值。

## 显示模块状态

先准备一个 `status_widget`，再实现 `show_status`：

```python
self.status_widget = QWidget()

def show_status(self, status):
    self.connection_label.setText(str(status.get("Connection", "—")))
```

后台测量代码可以这样更新状态：

```python
api.status({"Connection": "Connected", "State": "Ready"})
```

状态文字只用于显示。不能因为标签写着 “Connected” 就跳过真正的仪表读回。

## 从按钮请求后台动作

例如在 Status 页增加“测试连接”按钮：

```python
self.test_button.clicked.connect(
    lambda: self.api.action("test_connection")
)
```

窗口只发出请求。真正的连接测试由 `backend.py` 处理：

```python
def on_event(self, event, data, api):
    if event == "action" and data.get("name") == "test_connection":
        return self.test_connection(api)
```

具体的 `data` 内容可直接参考教学模块，不必从零猜测。

## 窗口代码不要做这些事

- 不要打开 VISA、串口或厂商软件连接；
- 不要创建线程访问仪表；
- 不要直接写配置或 DAT 文件；
- 不要控制 SEQ 或其他仪表；
- 不要在 `load()` 中自动应用设置。

??? example "展开教学模块的完整 frontend.py"

    ```python
    --8<-- "plugin_templates/measurement-modules-repository/modules/tutorial_resistance/frontend.py"
    ```
