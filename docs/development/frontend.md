# 开发 Settings 与 Status 界面

没有 `frontend.py` 时，核心仍能运行后端，并显示通用 Settings/Status 页。只有仪表确实
需要自定义参数时才增加 QWidget。

## 最小 Frontend

```python
from PySide6.QtWidgets import QWidget


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def load(self, settings):
        """只把数据填入控件。"""

    def dump(self):
        """返回当前控件值。"""
        return {}
```

`load` 和 `dump` 是唯一必需方法。类名固定为 `Frontend`，也可以把作者类最后赋值给它。

## 核心负责的行为

模块前端不需要重复实现：

- Settings/Status 标签页；
- Apply Settings 按钮；
- 未应用设置比较和 Run 前提示；
- Run 期间禁用 Settings；
- 模块窗口不能由用户直接关闭；
- 初始窗口宽度适配内容并避免横向滚动条；
- 未展开下拉列表时阻止滚轮误改值。

## Status 页

把任意 QWidget 放在 `status_widget`：

```python
self.status_widget = QWidget()

def show_status(self, status):
    self.connection_label.setText(str(status.get("Connection", "—")))
```

后端通过 `api.status(mapping)` 推送小型只读映射。前端只能消费状态，不能把标签变化当作
仪表安全读回。

## 用户动作

Status 页按钮可以请求后端动作：

```python
self.test_button.clicked.connect(
    lambda: self.api.action("test_connection")
)
self.refresh_button.clicked.connect(self.api.refresh)
```

后端在 `on_event("action", data, api)` 或 `on_event("status", {}, api)` 中处理。Action 只在
Idle 允许，返回值仍经过 worker 边界。

## 绝对禁止的行为

Frontend 在 GUI 主进程运行，因此不得：

- 打开 VISA、串口或厂商 SDK；
- 创建工作线程访问仪表；
- 写配置、DAT 或实验文件；
- 控制温度、磁场或 SEQ；
- 在 `load()` 中自动 Apply 保存设置；
- 持有后端或仪表对象引用。

??? example "展开教程模块的完整 frontend.py"

    ```python
    --8<-- "plugin_templates/measurement-modules-repository/modules/tutorial_resistance/frontend.py"
    ```

加载 SEQ 伴随设置时，核心只调用 `load()` 更新界面。模块仍保持 Disabled，或保持当前
Enabled 但未 Apply 状态，不会因为打开文件而向仪表发送命令。
