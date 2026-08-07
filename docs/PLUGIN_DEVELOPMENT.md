# 扩展开发

OpenLab Control 只有两类扩展：Device Plugin 负责温度、磁场和只读监视设备；
Measurement Module 负责一次完整测量。扩展是本地可执行代码，不是安全沙箱。

## Measurement Module：最小目录

```text
modules/my_meter/
├─ module.toml
└─ backend.py
```

```toml
name = "My Meter"
version = "0.1.0"
```

目录名就是模块 ID。入口固定为 `backend.py` 的 `Module`，无需声明入口、API 版本、
调度模式或 DAT 列。

除这些边界外，模块可自由增加内部文件、类和相对导入，协议命令及仪表状态机完全由作者
组织；核心不要求目录分层、基类、Mixin 或框架提供的驱动封装。

## 后端只有三个必需方法

```python
from labcontrol.module_api import ModuleAPI, ModuleError


class Module:
    columns = {"Resistance": "Ohm", "StatusCode": ""}

    def open(self, api: ModuleAPI):
        self.instrument = open_instrument(timeout=3.0)
        api.status({"State": "Ready"})

    def measure(self, slot: int, api: ModuleAPI):
        api.sleep(0)  # 检查 Pause/Stop
        value = self.instrument.read()
        return {"Resistance": value, "StatusCode": 0}

    def close(self, api: ModuleAPI):
        self.instrument.output_off()
        self.instrument.close()
```

无需继承框架基类。三个方法可为普通函数，也可返回 awaitable：

- `open(api)`：Enable 时调用；只建立安全的初始状态，不会收到保存设置。
- `measure(slot, api)`：每次返回一行 Mapping。
- `close(api)`：Disable 和应用退出时调用；必须幂等，并尽力进入仪表安全状态后释放资源。

`columns` 是有序的 `{列名: 单位}`。核心校验列名、JSON 标量、有限数值和消息大小，
不解释模块状态码。

## 三个可选能力

只在需要时添加：

```python
def configure(self, settings, api):
    """用户明确点击 Apply Settings 时调用。"""

def on_event(self, event, data, api):
    """处理 run_start、run_end、status 或 action。"""

slots = 4  # 也可以是 (1, 2, 4) 或动态 property
```

`on_event` 的输入固定为：

- `run_start`：`data == {}`；第一条 SEQ 指令前调用。
- `run_end`：`data["reason"]` 为 `completed`、`stopped` 或 `error`。
- `status`：用户或 Run 起始快照请求实际状态。
- `action`：`data == {"name": str, "payload": dict}`，仅 Idle 时允许。

没有 `on_event` 时这些事件都是无操作；不支持的自定义 action 会返回 Warning。

## 多通道与返回值

- `slots = 4` 表示槽位 1–4；序列也可只列出启用槽位。
- 核心取所有模块槽位的并集，按槽位写多行；同一槽位的不同模块并行。
- 没有 `slots` 的模块跟随每个逻辑槽位调用一次；若所有模块都未声明，一次 Measure
  只有槽位 1。
- 当前槽位直接由 `measure(slot, api)` 的 `slot` 参数给出；模块内部不要循环产生多行。
- 普通结果返回 Mapping；需要 rawdata 时返回 `(row, raw_values)`。

```python
return (
    {"Resistance": mean, "StatusCode": 0},
    original_voltage_readings,
)
```

模块不写 DAT，也没有 `emit_row`。未测量列直接省略。数据异常时，模块应省略无效测量值、
写入自己定义的数值状态码并用 `api.warn(...)` 报告；系统/通信/安全状态无法确认时抛出
`ModuleError` 或其他异常，使 SEQ 中止。

## ModuleAPI

作者通常只需五项能力：

- `api.sleep(seconds)`：Pause 不计时、Stop 可打断；`sleep(0)` 只做一次检查。
- `api.devices()`：获取最新温度、磁场和 Monitor 快照的副本。
- `api.warn(code, message, key="")`：报告可恢复 Warning；`message=None` 解除同一告警。
- `api.status(mapping)`：更新模块只读状态。
- `api.timeout`：本次核心操作总上限，用于给安全清理预留时间。

需要终止当前调用但允许 SEQ 继续时可抛 `ModuleWarning`；致命故障可抛
`ModuleError(message, code, key)`。未处理异常也按 Error 处理。

每个 VISA、串口或厂商 SDK 调用仍必须设置有限 I/O timeout。核心终止 worker 只能回收
本机进程，不能证明真实仪表已经关闭输出。

## 可选界面

没有 `frontend.py` 时框架显示通用页。自定义界面就是普通 QWidget：

```python
from PySide6.QtWidgets import QWidget


class Frontend(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        # 在这里创建普通 PySide6 控件。

    def load(self, settings):
        """把保存或随 SEQ 导入的设置填入控件；不得操作仪表。"""

    def dump(self):
        """返回当前控件值。"""
        return {}
```

只有 `load` 和 `dump` 必需。可选：

- `status_widget`：任意 QWidget，作为 Status 页。
- `show_status(mapping)`：刷新只读状态。
- `self.api.action(name, payload)`：请求后端 `on_event("action", ...)`。
- `self.api.refresh()`：请求 `on_event("status", ...)`。

Apply 按钮、窗口关闭规则、未应用设置比较和 Run 期间 Settings 禁用由核心负责。前端不
注册“设置改变”信号，不创建线程，不连接仪表，不写文件，也不控制温度或磁场。

## 仪表安全测试不得省略

真实模块至少测试：

- 地址、身份、量程、限流/限压、互锁和关键设置读回不符；
- 命令顺序，以及写超时后不盲目重放；
- 正常、超量程、compliance、损坏响应和模块自定义状态码；
- Pause/Stop、每种 `run_end`、重复 `close`、异常中的安全清理；
- I/O timeout 小于核心总时限，并为关闭输出预留时间；
- 多通道槽位、空列、rawdata、NaN/Infinity 和并行模块；
- 设置保存/SEQ 导入只更新界面，不自动 Apply。

软件测试不能替代真机低风险验证、仪表硬件保护和人工急停。

## Device Plugin

Device Plugin 仍使用 `DevicePlugin` 接口，因为核心必须统一执行控制上限、失联恢复和
Hold：

```python
class MyController(DevicePlugin):
    async def connect(self): ...
    async def poll(self): ...
    async def set_target(self, value, rate_per_minute, mode="Settle"): ...
    async def hold(self): ...
    async def disconnect(self): ...
```

GUI 不直接操作设备。主配置限制手动控制、SEQ 参数窗口和运行时执行；插件还应再次检查
设备自身边界。写命令超时视为结果不确定，不自动重发。

## 依赖与离线安装

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 使用框架锁定版本，扩展
不得重复声明。只有额外库才写入 `dependencies`，并携带精确带 SHA-256 的
`requirements.lock` 和全部本地 wheel。安装固定使用 `--no-index --require-hashes`。

安装时复制完整模块目录到 `modules/`，重启并核对来源、版本和内容指纹。不要提交仪表
地址密码、令牌、私钥、实验 DAT、`plugin_runtime`、`plugin_state` 或 `module_data`。
