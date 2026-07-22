# 设备插件与测量模块完整开发工作流

OpenLab Control 有两种扩展方式：

- Device Plugin：一个温度控制器、磁体电源或只读 Monitor，由主 Runtime 统一轮询和判稳。
- Measurement Module：一个完整测量方案，可协调多台源表、纳伏表、切换器、AC Bridge 等，拥有自定义 Settings/Status UI 和独立工作进程。

测量仪表不能再配置成 `kind = "measurement"`。新项目先判断责任边界，再选择下面一条工作流。

## 1. 选择扩展类型

| 需求 | 使用 |
|---|---|
| 设置并稳定样品温度 | temperature Device Plugin |
| 设置并稳定磁场 | field Device Plugin |
| 只读二级冷头、压力、液位等单值 | monitor Device Plugin |
| 一台表完成一套测量 | Measurement Module |
| 多台表并行/顺序协调、切换通道、输出电流、采集 R1–R4 | Measurement Module |
| 自定义 Settings/Status/手动操作 | Measurement Module |

Measurement Module 可以读取控制/Monitor 快照，但不能设置温度或磁场。温场流程必须由 SEQ 管理。

---

# A. Device Plugin 工作流

## A1. 从模板开始

- 控制器：`plugin_templates/controller_plugin.py`
- Monitor：`plugin_templates/monitor_plugin.py`
- 测试骨架：`plugin_templates/test_plugin.py`

将文件复制到受版本管理的 Python 包，例如：

```text
src/labcontrol_plugins/
└─ lakeshore336.py
```

类必须继承 `labcontrol.devices.base.DevicePlugin`。

## A2. 生命周期

```python
class MyController(DevicePlugin):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def poll(self) -> DeviceSnapshot: ...
    async def set_target(self, value, rate_per_minute, mode="Settle") -> None: ...
    async def hold(self) -> None: ...
```

- `__init__`：只保存配置，不打开硬件。
- `connect`：打开资源、验证型号/固件、设置通信超时，不改变危险输出。
- `poll`：返回单调时间戳、连接状态、current/target/rate/activity；不得自行判 Stable。
- `set_target`：框架已经做上下限/速率检查，驱动仍应保留设备侧检查。
- `hold`：停止变化并保持当前值，不等同于关机。
- `disconnect`：释放句柄，尽量幂等。

阻塞库应通过 `asyncio.to_thread()` 调用，并在 VISA、串口、TCP 或 SDK 本身设置有限超时。不要用无限重试掩盖断线。

## A3. 快照示例

```python
return DeviceSnapshot(
    device_id=self.config.id,
    display_name=self.config.display_name,
    kind=self.config.kind,
    timestamp=time.monotonic(),
    connected=True,
    unit=self.config.unit,
    current=current,
    target=target,
    rate_per_minute=rate,
    activity=DeviceActivity.MOVING,
)
```

Monitor 只返回 current，不返回 target/rate，也不实现 set_target/hold。

## A4. 配置

```toml
[[devices]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
plugin = "labcontrol_plugins.lakeshore336:Lakeshore336Controller"
unit = "K"
min_value = 1.8
max_value = 400.0
default_rate_per_minute = 5.0
max_rate_per_minute = 30.0
stability_tolerance = 0.05
stability_max_slope_per_minute = 0.03
stability_dwell_seconds = 30.0
stability_timeout_seconds = 1800.0
stability_window_seconds = 20.0
address = "GPIB0::12::INSTR"
```

未知键进入 `config.extras`。安全上限必须写在主配置，不能只藏在驱动 UI。

## A5. 错误映射

```python
raise DeviceWarning("Reading is near range limit", "NEAR_RANGE", channel)
raise DeviceError("Controller reported sensor fault", "SENSOR_FAULT", input_name)
raise SafetyViolation("Target exceeds local interlock", "LOCAL_LIMIT", device_id)
```

- Warning：可恢复，SEQ 继续。
- DeviceError/SafetyViolation：运行中触发 fatal Stop。
- code/context 必须稳定，才能让同一活动事件只弹一次。

---

# B. Measurement Module 工作流

## B1. 复制完整模板

复制：

```text
plugin_templates/measurement_module/
```

到：

```text
modules/my_measurement/
├─ module.toml
├─ frontend.py
├─ backend.py
└─ wheels/              可选，模块专用离线 wheel
```

参考实现：`modules/simulated_transport/`。

模块文件夹名不是权威 ID；权威 ID 在 `module.toml`。源码目录只放代码/清单/可选 wheel，用户参数保存在 `module_data/<id>/settings.toml`。

## B2. 设计仪表所有权

在编码前列出完整方案：

```text
模块：dc_transport
├─ Keithley 6221：电流源
├─ Keithley 2182A：纳伏表
├─ Keithley 7001：通道切换
└─ 流程：R1 → R2 → R3 → R4，每个结果立即发一行
```

原则：

- 这些仪表只归该模块 backend 所有，不同时配置成 Device Plugin。
- 所有硬件句柄只在工作进程创建和使用。
- frontend 不能导入/持有 VISA 资源。
- 多模块共享同一台物理仪表时，必须先设计跨进程仲裁；0.10.0 不提供隐式共享锁，默认禁止这种配置。

## B3. 编写 `module.toml`

```toml
id = "dc_transport"
name = "DC Transport"
version = "1.0.0"
api_version = "1.0"
frontend = "frontend:DcTransportFrontend"
backend = "backend:DcTransportBackend"
backend_type = "python"
dependencies = [
    "pyvisa==1.14.1",
    "pyserial>=3.5,<4",
]

[[columns]]
name = "R1"
unit = "Ohm"

[[columns]]
name = "Status"

[[columns]]
name = "Warning"
```

约束：

- ID 必须匹配 `[a-z][a-z0-9_]*` 且全局唯一。
- `api_version` 当前必须是 `1.0`。
- 0.10.0 只支持 `backend_type = "python"`；未来 executable backend 会使用同一生命周期语义。
- 至少声明一列；列名唯一、单行、不能含逗号。
- Run 中不能动态增加列。
- 模块应声明自己的 Status/Warning 列；框架不会替它添加。

## B4. 依赖策略

所有模块共用：

```text
module_runtime/site-packages/
```

不要把每个模块打包成独立 venv，这会增大体积和启动时间。推荐流程：

1. 在开发机统一确定并锁定版本。
2. 把可离线分发的 wheel 放入根 `wheels/`；仅模块专用 wheel 可放模块自己的 `wheels/`。
3. 在 Modules Manager 选择模块，点击 `Install Dependencies`。
4. 程序先执行离线 pip `--target module_runtime/site-packages`。
5. 离线失败时由用户明确确认是否在线安装。
6. Refresh；依赖满足后才允许 Enable。

同一包的版本范围不相交时，两个模块都标记冲突并禁止 Enable。开发者必须调整版本或替换库；框架不会偷偷为它们加载两份冲突依赖。

框架自身核心依赖（PySide6、QtAwesome、packaging）应由主 requirements 管理。模块不要要求不兼容的 PySide6 版本。

## B5. Backend 契约

```python
from labcontrol.measurement.api import ModuleBackend, ModuleOperationContext

class DcTransportBackend(ModuleBackend):
    def initialize(self, settings, context): ...
    def apply_settings(self, settings, context): ...
    def begin_sequence(self, context): ...
    def measure(self, context): ...
    def end_sequence(self, reason, context): ...
    def abort(self, context): ...
    def read_status(self, context): ...
    def manual_action(self, action, payload, context): ...
```

方法通常是同步函数，因为它们已经运行在独立进程；框架也接受返回 awaitable 的实现。所有返回值必须是简单 Mapping 或 None，才能通过 IPC 序列化。

### `initialize(settings, context)`

Enable 自动调用：

- 打开并识别所有仪表；
- 建立模块内部状态；
- 读取必要的实际状态；
- 把保存的 settings 当作 desired 值加载；
- **不得把保存 settings 自动发送到仪表**。

成功后返回 Status，例如 `{"Connection": "Connected", "Applied Settings": "Not applied"}`。失败抛 `ModuleError`，模块保持 Disabled。

### `apply_settings(settings, context)`

用户点击 `Apply Settings` 并确认后调用。验证整组参数，再按安全顺序发送；成功后返回实际/Applied 状态。函数失败时窗口和模块保持 Enabled，Settings 仍标为未 Apply。

### `begin_sequence(context)`

Run 开始、第一条 SEQ 之前调用。用于打开源输出、清零缓存、进入远程测量状态。不要在此改变温度或磁场。

### `measure(context)`

每条无参数 `T Measure` 调用一次。可以：

```python
def measure(self, context):
    for channel in ("R1", "R2", "R3", "R4"):
        value = self.read_channel(channel)
        context.emit_row({channel: value, "Status": "OK", "Warning": ""})
```

每个 `emit_row()` 立即经 IPC 到中央写盘，不必等 R1–R4 全部完成。模块也可返回一个 Mapping 形成单行，但不能同时返回动态 Schema。

### `end_sequence(reason, context)`

每次 Run 最终都调用，reason 固定为：

- `completed`
- `stopped`
- `error`

这里关闭源输出、退出扫描/触发状态、恢复模块自己的安全待机状态。失败会把最终运行标记为 Faulted，模块保持 Enabled 供用户检查；框架不自动调用 abort。

### `abort(context)`

仅在用户 Disable 或整个应用退出时调用。用于彻底停止输出、释放仪表状态。Disable 时 abort 失败会使模块保持 Enabled，窗口不隐藏。

### `read_status(context)`

返回一组当前实际状态。Run 开始前会调用一次并保存为 `status-at-start.json`；Status 页刷新也可调用。不要在读状态时触发设置。

### `manual_action(action, payload, context)`

实现 Test Connection、Read Now、Measure Now 等模块自定义操作。仅 SEQ Idle 可用。成功结果更新 Status 并写事件日志，不写实验 DAT。

## B6. Context

`context.system` 是只读字典副本：

```python
temperature = context.system.get("temperature", {}).get("current")
field_oe = context.system.get("field", {}).get("current")
second_stage = context.system.get("second_stage", {}).get("current")
```

每个设备包含 display_name、kind、timestamp、connected、unit、current、target、rate、activity、stability、message。修改字典不会影响中央状态。

可用输出：

```python
context.emit_row({...})
context.update_status({...})
context.warning("R1 over range", "OVER_RANGE", "R1")
context.resolve_warning("OVER_RANGE", "R1")
context.error("Interlock opened", "INTERLOCK_OPEN", "source")
```

`context.error()` 会抛 `ModuleError`。也可直接抛：

```python
raise ModuleWarning("Reading overloaded", "OVER_RANGE", channel)
raise ModuleError("Source reported hardware fault", "SOURCE_FAULT", address)
```

Warning 的 code/context 应跨测量点保持稳定，避免把同一故障变成无数不同弹窗。

## B7. Frontend 契约

```python
from labcontrol.measurement.frontend_api import ModuleFrontend

class DcTransportFrontend(ModuleFrontend):
    def create_settings_page(self, parent=None): ...
    def create_status_page(self, parent=None): ...
    def settings(self): ...
    def load_settings(self, settings): ...
    def update_status(self, status): ...
    def set_sequence_running(self, running): ...
```

模块完全自定义两页内部布局；框架不要求统一控件。必须遵守：

- Settings 是默认页；
- 参数变化时发 `self.settingsChanged`；
- `load_settings()` 只更新控件，不发送仪表设置；
- `settings()` 只返回 TOML 可保存的 bool/int/有限 float/string/list/嵌套 dict；
- `update_status()` 只显示后台返回值；
- `set_sequence_running(True)` 禁用 Test/Read/Measure 等手动按钮；
- 不直接导入 pyvisa/serial、不开线程持有仪表、不写 DAT。

请求手动动作：

```python
self.context.request_manual_action("measure_now", {"channel": "R1"})
self.context.request_status_refresh()
```

框架窗口已经负责 Settings/Status 页签、Apply 确认、运行锁定、不可关闭和父窗口关系。

## B8. Settings 保存语义

框架在以下时间自动写 `module_data/<id>/settings.toml`：

- Apply；
- Disable；
- 应用关闭（先保存，后 abort）；
- Run 之前。

Enable 时读取保存值到 Settings 和 backend initialize，但不触发 Apply。Run 时如果用户在 UI 修改后尚未 Apply，会出现：

- `Apply and Run`
- `Run Without Applying`
- `Cancel`

无论选择前两项哪一个，运行目录保存的是当时 Settings 页的 desired 值；实际仪表状态另存 JSON。

## B9. 多仪表并行与内部并发

不同 Enabled 模块由框架并行调用。一个模块内部是否并行由模块自己决定：

- R1–R4 依赖同一切换器时通常顺序执行；
- 两台完全独立表可用线程/asyncio 并发，但仍需保证 `emit_row` 顺序符合业务含义；
- 不要让后台线程在 measure 返回后继续发实验行；返回表示本模块本次 Measure 完成。

同一模块的 IPC 请求串行，因此 Apply、manual_action 和 measure 不会在框架层互相重入。

## B10. 通信超时与恢复

框架不包一层固定生命周期超时，因为不同仪表操作时长差异很大。模块必须：

- 给每次读写设置有限且合理的协议超时；
- 把临时超量程/无效点映射为 Warning；
- 把设备报警、掉线、互锁、二级冷头过温等映射为 Error；
- 对写操作谨慎重试，避免重复设置/触发；
- 让 abort 和 end_sequence 尽量幂等；
- 在 Status 中显示 Fault/Connection/Output 等实际状态。

不要无限 `while True` 等待设备；这会同时阻塞 Measure、Disable 和应用退出。

## B11. 测试顺序

1. 清单测试：ID、API、列、依赖、冲突。
2. Backend 纯仿真：每个生命周期返回值和异常映射。
3. Frontend offscreen：Settings round-trip、Status 更新、SEQ 锁定。
4. Service 测试：独立进程 Enable/Apply/Measure/End/Disable。
5. Schema 测试：未知列、复杂值、缺失值、多行顺序。
6. Warning 测试：继续执行、只弹一次、events.dat 有 Count/Context。
7. Error 测试：SEQ Faulted、执行 end_sequence(error)、不调用 abort。
8. Disable abort 失败测试：仍 Enabled、窗口可见。
9. 多模块并行：各模块内部顺序保持，中央 DAT 无并发写损坏。
10. 真实硬件分阶段测试：只读 → 最小输出 → Stop/Error → 长时运行。

运行仓库测试：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

专项参考：`tests/test_measurement_modules.py` 和 `tests/test_engine.py`。

## B12. 发布模块

发布前：

- 提升模块 `version`，不要随意改变 `id`；
- 固定依赖版本范围，准备 Windows/目标 Python 匹配的 wheel；
- 不提交 `module_data`、仪表地址中的秘密或运行 DAT；
- 更新模块自己的变更记录和操作说明；
- 在干净环境执行 Install Dependencies、Refresh、Enable 和完整生命周期测试；
- 确认打包发布目录已包含模块源码和所需共享 wheels。

0.10.0 推荐保留源代码，便于实验室审查和修改。未来 executable backend 可隐藏/隔离厂商运行时，但会增加部署、协议版本和诊断复杂度；在 API 稳定前不建议优先采用。

## B13. 修改现有模块的安全流程

1. Stop 当前 SEQ。
2. Disable 所有模块，确认 abort 成功且窗口隐藏。
3. 备份 `module_data/<id>/settings.toml`。
4. 修改源码/清单并运行单元测试。
5. 若 Schema 改变，提升模块版本并记录 DAT 列迁移。
6. 若依赖改变，更新 wheels 并执行 Install Dependencies。
7. 在 Modules Manager 点击 Refresh。
8. Enable，检查 Settings 只加载未 Apply；检查 Status 实际状态。
9. 用仿真/低风险 SEQ 验证 begin/measure/end/abort。
10. 再进入真实实验。

## 完成定义

一个真实扩展只有同时满足以下条件才算完成：

- 安全限制和通信超时明确；
- 生命周期在 completed/stopped/error/disable/exit 全路径可预测；
- Warning/Error code/context 稳定且有测试；
- Settings 与实际 Status 分开保存；
- 模块列固定、有单位、有模块前缀；
- 断线、超量程、Stop、Error、end/abort 失败均经过验证；
- 文档说明仪表接线、地址、输出风险、恢复和依赖安装步骤。
