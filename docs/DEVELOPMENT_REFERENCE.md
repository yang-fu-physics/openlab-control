# 开发参考

OpenLab Control 有两套分开的接入方式：System Instrument 负责温度、磁场和只读监视仪表；
Measurement Module 负责一次完整测量。两者都是本地可执行代码，进程隔离不是安全沙箱。

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
    display_columns = ("Resistance",)  # 可选：主窗口卡片显示的已有列

    def __init__(self):
        self.instrument = None

    def open(self, api: ModuleAPI):
        try:
            self.instrument = open_instrument(timeout=3.0)
            api.status({"State": "Ready"})
        except Exception:
            self._release()
            raise

    def measure(self, slot: int, api: ModuleAPI):
        api.sleep(0)  # 检查 Pause/Stop
        value = self.instrument.read()
        return {"Resistance": value, "StatusCode": 0}

    def close(self, api: ModuleAPI):
        self._release()

    def _release(self):
        instrument = self.instrument
        self.instrument = None
        if instrument is None:
            return
        try:
            instrument.output_off()
        finally:
            instrument.close()
```

无需继承框架基类。三个方法可为普通函数，也可返回 awaitable：

- `open(api)`：Enable 时调用；只建立安全的初始状态，不会收到保存设置。
- `measure(slot, api)`：每次返回一行 Mapping。
- `close(api)`：Disable 和应用退出时调用；必须幂等，并尽力进入仪表安全状态后释放资源。

`open_instrument` 自己也必须清理“连接过程中尚未返回就失败”的内部资源。上面的 `_release`
先断开对象引用，再用 `finally` 保证即使关闭输出失败也尝试释放连接，因此重复 `close` 不会
再次操作同一对象。

`columns` 是有序的 `{列名: 单位}`。核心校验列名、JSON 标量、有限数值和消息大小，
不解释模块状态码。

可选 `display_columns` 是最多八个现有列名。核心把每次已经校验的结果缓存到主窗口卡片，
不会再次调用模块或仪表。声明 `slots` 时按逻辑槽位显示；未声明时只保留最新一行。
不声明不会产生兼容提示，也不影响 DAT。

## 常用可选能力

只在需要时添加：

```python
class Module:
    slots = 4  # 也可以是 (1, 2, 4) 或动态 property

    def configure(self, settings, api):
        """用户明确点击 Apply Settings 时调用。"""

    def on_event(self, event, data, api):
        """处理 run_start、run_end、status 或 action。"""

        if event == "run_end":
            self.instrument.output_off()
            return {
                "Output": "Off",
                "Last Run": data.get("reason", "—"),
            }
        return {}
```

`on_event` 是可选方法，但只要模块可能在两次 `Measure` 之间保持输出，就必须处理
`run_end`。默认应关闭输出。若模块需要让连续偏置跨 SEQ 保持，可以提供默认勾选的关闭
选项；用户取消后，`run_end` 必须读回输出和所有关键设置，确认无误才保持，下一次
`run_start` 也不能制造一次短暂掉电。Apply、Disable、退出、测量异常或状态无法确认时
仍要关闭。每次 SEQ 结束不会调用 `close`；`close` 是 Disable/退出时始终关闭输出的保障。

`on_event` 的输入固定为：

- `run_start`：`data == {}`；第一条 SEQ 指令前调用。
- `run_end`：`data["reason"]` 为 `completed`、`stopped` 或 `error`。
- `status`：用户或 Run 起始快照请求实际状态。
- `action`：`data == {"name": str, "payload": dict}`，仅 Idle 时允许。

没有 `on_event` 时这些事件都是无操作；不支持的自定义 action 会返回 Warning。

## 可选：向 Sequence Command Bar 注册模块指令

模块不需要修改核心枚举或解析器。仅在确有独立仪表动作时，在 `Module` 上增加纯声明
`sequence_commands` 和一个统一处理方法：

```python
class Module:
    columns = {"Resistance": "Ohm", "StatusCode": ""}

    sequence_commands = [
        {
            "id": "set_current",          # 模块版本间保持稳定
            "label": "Set Current",
            "description": "Set output current without writing a DAT row.",
            "kind": "command",            # 省略时也是 command
            "fields": [
                {
                    "name": "current",
                    "label": "Current",
                    "type": "float",
                    "default": 1e-3,
                    "minimum": -10e-3,
                    "maximum": 10e-3,
                    "unit": "A",
                    "decimals": 9,
                },
                {
                    "name": "output",
                    "label": "Enable output",
                    "type": "bool",
                    "default": True,
                },
            ],
        },
        {
            "id": "scan_current",
            "label": "Scan Current",
            "kind": "scan",
            "points_field": "points",
            "point_parameter": "current",
            "fields": [
                {
                    "name": "points",
                    "label": "Current points",
                    "type": "list",
                    "default": ["1 mA", "2 mA", "5 mA"],
                },
                {
                    "name": "settle_seconds",
                    "label": "Settle time",
                    "type": "float",
                    "default": 0.0,
                    "minimum": 0.0,
                    "unit": "s",
                },
            ],
        },
    ]

    def execute_sequence_command(self, command_id, parameters, api):
        # 参数窗口不是安全边界；发送 SCPI 前必须在后端再次验证范围、状态和单位。
        if command_id == "set_current":
            current = float(parameters["current"])
            self._validate_current(current)
            self.instrument.set_current(current)
            return {"Current": current}
        if command_id == "scan_current":
            # 核心每个点调用一次，并把当前点放在 point_parameter 指定的键中；完整
            # points 列表和其他公共参数仍保留，便于模块记录上下文。
            current = parse_current(parameters["current"])
            self._validate_current(current)
            self.instrument.set_current(current)
            api.sleep(float(parameters["settle_seconds"]))
            return {"Current": current}
        raise ModuleError(
            f"Unknown command: {command_id}",
            "UNKNOWN_SEQUENCE_COMMAND",
            command_id,
        )
```

声明规则：

- 模块必须先成功 Enable，核心才在右侧直接增加以模块 `name` 命名的顶层组；Disable
  立即移除。发现阶段不会为了菜单导入模块源码。
- `id`、字段 `name`、`points_field` 和 `point_parameter` 都使用
  `[a-z][a-z0-9_]*`；`id` 是写入 SEQ 的兼容标识，不能只因修改显示文字而更换。
- 支持的字段类型只有 `text`、`int`、`float`、`choice`、`bool`、`list`。每个字段必须
  有 JSON 默认值；`choice` 需要字符串 `choices`；数值可给 `minimum/maximum/unit/decimals`。
- `list` 是最多 100,000 项的 JSON 标量数组，可用字符串保存 `1 mA`、`1 pA` 等作者
  自定义单位写法。真正的语法和物理范围仍由模块后端解析、验证。
- `kind = "scan"` 必须指定一个 `list` 类型的 `points_field`。核心按原顺序逐点调用处理
  方法，把该点另存到 `point_parameter`，成功后执行该 Scan 的子指令。
- 普通指令和扫描点处理只允许返回状态 Mapping 或 `None`，不会产生 DAT 行。需要测量时
  在扫描子树中显式放置无参数 `Measure`。
- 同一模块的指令、`measure` 和生命周期请求严格串行；不同模块在一次 `Measure` 中仍
  可并行。不要在处理方法中另建线程并发访问同一个 VISA session。
- 抛 `ModuleWarning` 时当前普通动作结束并继续下一条；扫描中该点的子树不执行。抛
  `ModuleError`、通信错误或未处理异常时整个 SEQ 中止并进入既有 `run_end("error")`。

保存后的通用语法为：

```text
T Module Command "my_meter" "set_current" {"current":0.001,"output":true}
T Module Scan "my_meter" "scan_current" {"points":["1 mA","2 mA"],"settle_seconds":0}
T     Measure
T End Scan
```

模块未安装、Disabled 或删除了旧指令 ID 时，框架仍解析和保存这些行，只在界面标红并给
Warning；Run 预检拒绝执行，绝不会自动 Enable、Apply、安装或静默跳过一个启用的模块
指令。不要提供通用“任意 SCPI”指令，这会绕过可审查的参数和安全状态机。

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

作者通常只需六项能力：

- `api.sleep(seconds)`：Pause 不计时、Stop 可打断；`sleep(0)` 只做一次检查。
- `api.checkpoint()`：长循环或两次仪表 I/O 之间立即检查 Pause/Stop。
- `api.instruments()`：让核心立即取得一次测量专用温场快照；它不受前面板常规刷新周期限制，
  同一时刻多个模块请求会合并。System Instrument 可用 `poll_measurement()` 只读主值；未实现
  时仍调用完整 `poll()`。
- `api.warn(code, message, key="")`：报告可恢复 Warning；`message=None` 解除同一告警。
- `api.status(mapping)`：更新模块只读状态。
- `api.timeout`：本次核心操作总上限，用于给安全清理预留时间。

需要终止当前调用但允许 SEQ 继续时可抛 `ModuleWarning`；致命故障可抛
`ModuleError(message, code, key)`。未处理异常也按 Error 处理。

每个 VISA、串口或厂商 SDK 调用仍必须设置有限 I/O timeout。核心终止 worker 只能回收
本机进程，不能证明真实仪表已经关闭输出。

即使模块没有调用 `api.instruments()`，核心也会在每个测量槽位写 DAT 前取得即时系统快照。
如果模块刚在 0.1 秒内读过，写行会复用该样本，避免连续重复查询慢速温控器。

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
- `edit_sequence_command(command_id, parameters)`：仅当对应声明设置
  `custom_editor = true` 时打开作者自定义的模态参数窗口；取消返回 `None`，接受返回
  Mapping。核心仍按声明字段重新验证返回值。该方法只编辑参数，不得连接或操作仪表。

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
- 模块指令 ID/参数声明、普通动作、逐点 Scan、缺失/Disabled 预检、Warning 跳点、
  Error 收尾，以及自定义参数窗口返回非法值；

软件测试不能替代真机低风险验证、仪表硬件保护和人工急停。

## System Instrument

System Instrument 仍使用 `SystemInstrument` 接口，因为核心必须统一执行控制上限、失联恢复和
Hold：

```python
class MyController(SystemInstrument):
    async def connect(self): ...
    async def poll(self): ...
    # 可选；完整 poll 很慢时只读取写测量行需要的主值
    async def poll_measurement(self): ...
    async def set_target(self, value, rate_per_minute, mode="Settle"): ...
    async def hold(self): ...
    async def disconnect(self): ...
```

GUI 不直接操作仪表。主配置限制手动控制、SEQ 参数窗口和运行时执行；具体后端还应再次检查
仪表自身边界。写命令超时视为结果不确定，不自动重发。

同一物理仪表的辅助温度、加热功率或量程使用 `InstrumentSnapshot.metrics` 返回，不能为了多
显示一个值而对同一地址创建第二个通讯会话。`instrument_stable` 可作为核心独立误差、
斜率和 dwell 判定的附加必要条件，但不能替代这些条件。

`poll_measurement()` 没有实现时默认调用 `poll()`。若覆盖它，返回快照的仪表 ID、种类和
`metrics` 的 key/名称/单位/精度必须与完整快照相同；本次没有实际查询的附加值填 `None`。
常规 `poll()` 仍负责报警、联锁和安全状态，不能因为提供快速测量读取而停止执行。

同一 System Instrument 的方法不会并发执行。已经开始的完整仪表事务不会被抢占；它返回或
超时后，控制与安全操作优先，等待中的 `poll_measurement()` 再先于后台 `poll()`。后台
不得另开线程绕过核心队列并访问同一 VISA Session，否则这个保证不再成立。

## 依赖与离线安装

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 使用框架锁定版本，两类内容
不得重复声明。只有额外库才写入 `dependencies`，并携带精确带 SHA-256 的
`requirements.lock` 和全部本地 wheel。安装固定使用 `--no-index --require-hashes`。

安装时复制完整模块目录到 `modules/`，重启并核对来源、版本和内容指纹。不要提交仪表
地址密码、令牌、私钥、实验 DAT、`runtime_packages`、`trust_state` 或 `module_data`。
