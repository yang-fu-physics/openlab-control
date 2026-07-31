# Device Plugin 与 Measurement Module 开发

OpenLab Control 有两种扩展。选择错误会造成资源归属、超时和安全状态不清晰。

| 需求 | 扩展类型 |
|---|---|
| 设置并稳定样品温度 | temperature Device Plugin |
| 设置并稳定磁场 | field Device Plugin |
| 二级冷头、压力、液位等只读单值 | monitor Device Plugin |
| 一台或多台仪表组成的完整测量方案 | Measurement Module |
| 自定义 Settings/Status/手动测量 | Measurement Module |

Measurement Module 可以读取温度、磁场和 Monitor 快照，但没有设置温场的 API。温场流程
必须由 SEQ 和主设备插件管理。

## 1. 仓库布局

所有 Measurement Module 可放在同一个共享仓库。Device Plugin 的协议、状态和安全动作
随设备变化，核心发布内容只提供示例与接口骨架。不要为不同仪表维护 OpenLab Control
核心分支。

发布包中的 Git-ready 起点：

```text
plugin_templates/
├─ measurement-modules-repository/
│  ├─ modules/<module-id>/
│  └─ tests/
└─ device-plugin-examples/
   ├─ plugins/<plugin-id>/
   └─ tests/
```

安装不需要 Git 或网络。只把一个完整扩展目录复制到应用的 `modules/<id>/` 或
`device_plugins/<id>/`，然后重启。不要复制 `.git`、虚拟环境、测试输出、密钥或 runtime。

## 2. 共同的信任与依赖规则

外部扩展是本地可执行代码，不是沙箱。程序在导入源码前验证清单并计算整个扩展树的
SHA-256 指纹。首次加载要求用户确认类型、ID、版本、路径和指纹；任意源码、清单、lock
或 wheel 变化都会使旧信任失效。

### 2.1 清单共同字段

- `id`：必须匹配 `[a-z][a-z0-9_]*`。
- `name`：非空显示名。
- `version`：有效 PEP 440 版本；内容变化必须提升。
- `api_version`：Device Plugin 当前为 `"1.0"`；Measurement Module 当前为 `"1.1"`。
- `core_requires`：可选的 OpenLab Control 版本范围。
- 入口：只允许 `module_name:ClassName`，对应 `.py` 必须位于扩展根目录。
- `dependencies`：可选 PEP 508 要求；禁止 URL。

### 2.2 框架共享依赖与额外离线依赖

主框架统一提供并锁定 PySide6、QtAwesome、packaging、PyVISA 和
typing_extensions。扩展可以在 `dependencies` 中声明兼容范围；若核心版本满足，该项
自动归为框架共享，不需要 lock/wheel，也不会显示 Install Dependencies。若范围不兼容，
扩展在源码导入前直接判为 Invalid，不能用私有副本覆盖核心版本。

只有框架没有提供的额外依赖才必须携带 `requirements.lock`。例如模块声明
`vendor_sdk>=2,<3` 时，lock 应包含：

```text
vendor_sdk==2.4.1 --hash=sha256:<64 hex>
vendor_sdk_dependency==1.7.0 --hash=sha256:<64 hex>
```

额外依赖的所有直接和传递项都必须精确 `==`、带 SHA-256。禁止 `-r`、
`--index-url`、editable、URL、sdist 和未哈希项。把目标 Windows/Python 架构的
wheels 放在扩展 `wheels/`，或应用共享 `wheels/`。

安装固定执行：

```text
pip install --no-index --only-binary=:all: --require-hashes --target <staging>
```

验证完成后原子替换到：

```text
plugin_runtime/device/<id>/<fingerprint>/site-packages/
plugin_runtime/module/<id>/<fingerprint>/site-packages/
```

扩展子进程启动时再次验证额外依赖目录摘要。该路径只插入对应子进程，不进入主进程、
不执行 `.pth`，也不能覆盖框架共享依赖。没有额外依赖的扩展不应携带 lock/wheels。

## 3. Device Plugin

### 3.1 目录与清单

```text
plugins/my_controller/
├─ device.toml
├─ backend.py
├─ requirements.lock       可选
└─ wheels/                 可选
```

示例：

```toml
id = "my_controller"
name = "My Temperature/Field Controller"
version = "0.1.0"
api_version = "1.0"
core_requires = ">=0.11.0b1,<0.12"
backend = "backend:MyController"
kinds = ["temperature", "field"]
dependencies = []
```

一个插件可支持多个 kind，但每个配置设备实例都有独立子进程和独立后端对象。

### 3.2 生命周期

```python
class MyController(DevicePlugin):
    async def connect(self) -> None: ...
    async def poll(self) -> DeviceSnapshot: ...
    async def set_target(self, value, rate_per_minute, mode="Settle") -> None: ...
    async def hold(self) -> None: ...
    async def disconnect(self) -> None: ...
```

- `__init__`：只保存配置，不打开硬件、不改变输出。
- `connect`：设置有限协议超时，打开资源，核对型号/固件；不得自动 Apply 仪表设置。
- `poll`：返回新鲜单调时间戳、current、实际 target/rate/activity；Monitor 只返回 current。
- `set_target`：再次检查插件/仪表特有限制，然后只发送一次写命令。
- `hold`：读取新鲜当前值后使用厂商 Hold 或当前值目标；绝不能用缓存猜测或默认零。
- `disconnect`：幂等关闭所有句柄。

阻塞 VISA/串口/SDK 可以由插件在其设备子进程中调用，但底层仍必须设置比
`operation_timeout_seconds` 更短的超时。框架杀掉进程只能防止软件永久等待，不能保证
外部仪表进入安全状态。

### 3.3 配置选择与角色

复制插件后只改一份配置并重启：

```toml
[[devices]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
plugin = "my_controller"
role = "primary"
control_enabled = true
unit = "K"
min_value = 1.8
max_value = 400.0
default_rate_per_minute = 5.0
max_rate_per_minute = 30.0
operation_timeout_seconds = 10.0
shutdown_timeout_seconds = 3.0
address = "GPIB0::12::INSTR"
```

未知键进入 `config.extras`。每个 temperature/field kind 最多一个 primary；其他设备
使用 `role = "secondary"`，默认 `control_enabled = false`。Monitor 必须
`role = "monitor"` 且不可控。

主配置的上下限和最大速率同时约束手动弹窗、SEQ 参数编辑和运行时执行；插件应再保留
设备侧校验与硬件互锁。OpenLab Control 当前不提供仪表设置 UI，仪表面板初始设置由实验
人员手动完成。

### 3.4 失联和写超时

Poll/连接失败会终止旧设备进程，并按配置在 60 秒恢复窗内重建。恢复后读取并核对实际
target/rate；插件不得在 `connect` 中重放上次目标。SEQ 在主设备恢复期间冻结活动计时。

写超时属于“命令可能已执行”的歧义状态：框架不重试，插件也不应自动重发。应重新读取
实际状态、报告 Error，并让操作人员决定后续动作。

### 3.5 错误映射

```python
raise DeviceWarning("Reading near range", "NEAR_RANGE", channel)
raise DeviceError("Sensor fault", "SENSOR_FAULT", input_name)
raise SafetyViolation("Local interlock opened", "INTERLOCK_OPEN", device_id)
```

Warning 可恢复且 SEQ 继续；DeviceError/SafetyViolation 在运行中触发 fatal Stop。
code/context 必须稳定，才能正确去重。消息正文可以包含变化值，但不要把变化值放入
context。

## 4. Measurement Module

### 4.1 目录与清单

```text
modules/dc_transport/
├─ module.toml
├─ frontend.py
├─ backend.py
├─ requirements.lock       可选
└─ wheels/                 可选
```

```toml
id = "dc_transport"
name = "DC Transport"
version = "1.0.0"
api_version = "1.1"
core_requires = ">=0.11.5,<0.12"
frontend = "frontend:DcTransportFrontend"
backend = "backend:DcTransportBackend"
backend_type = "python"
measurement_mode = "aligned_slots"
dependencies = ["pyvisa>=1.14,<2"]

[[columns]]
name = "R1"
unit = "Ohm"

[[columns]]
name = "StatusCode"
```

这里的 PyVISA 范围由 OpenLab Control 0.11.1 提供的 1.16.2 满足，因此不需要
`requirements.lock`、wheel 或安装步骤。列在 Run 开始前固定，名字必须唯一、单行且
不含逗号。单结果组模块应显式声明整数 `StatusCode`；宽表汇总多个内部通道时可声明
`StatusCode1`、`StatusCode2` 等每组状态。中央写盘时自动加
`<module_id>.` 前缀。

`measurement_mode` 必须由正式模块显式声明：

- `aligned_slots` 用于需要与其他扫描模块按 CH1/CH2… 对齐的模块；在
  `begin_sequence` 后实现 `measurement_slots(context)` 返回本 Run 启用的唯一正整数
  槽位。
- `once_per_slot` 用于 2400 等单次模块，以及 2614B 这种一次调用便可汇总全部内部通道
  的模块。核心在每个逻辑通道槽位都重新调用一次。

缺少字段时发现界面会提示 Warning，核心为了第三方兼容仍按 `once_per_slot` 执行；新建
或正式发布的模块不得依赖这个兜底。

### 4.2 仪表所有权

一个模块拥有完成该测量所需的全部源表、表桥、切换器和内部时序。不要同时把这些物理
仪表配置成 Device Plugin，也不要让两个模块隐式共享同一仪表。需要共享时必须先设计
明确的跨进程仲裁服务；核心当前不提供隐式共享锁。

Frontend 只创建 PySide6 控件。所有 VISA/串口/SDK 句柄只在 backend 工作进程创建和
释放。

### 4.3 Backend 生命周期

```python
class DcTransportBackend(ModuleBackend):
    def initialize(self, settings, context): ...
    def apply_settings(self, settings, context): ...
    def begin_sequence(self, context): ...
    def measurement_slots(self, context): ...  # 仅 aligned_slots
    def measure(self, context): ...
    def end_sequence(self, reason, context): ...
    def abort(self, context): ...
    def read_status(self, context): ...
    def manual_action(self, action, payload, context): ...
```

- `initialize`：Enable 时加载并规范化 desired settings、发现资源，但不因保存设置而
  连接主仪表或发送设置。允许为了识别可选附件建立有限、只读的临时连接，但无论成功
  失败都要关闭，并在模块文档中说明降级行为。
- `apply_settings`：只在 Settings 页由用户确认后完整验证方案、连接/识别仪表并建立
  安全基线。应立即生效的设置必须发送并读回；只能在扫描时逐通道生效的参数可以延迟到
  `measure`，但使用前同样必须发送并读回。
- `begin_sequence`：第一条 SEQ 前准备模块输出/缓冲。
- `measurement_slots`：仅 `aligned_slots` 模块在每次 Run 的 `begin_sequence` 成功后调用
  一次；返回的槽位计划在本 Run 内冻结。
- `measure`：针对 `context.measurement_step.logical_slot` 执行一个测量单元，并且恰好
  产生一行。核心会按槽位多次调用，不允许模块自行循环发多行。
- `end_sequence(reason)`：对 completed/stopped/error 都关闭模块自身危险输出；普通
  Run 结束不调用 abort。worker 已超时、退出或 IPC 断开时不能保证此调用成功，核心
  必须保留 Safety Unconfirmed，而不能把进程回收等同于仪表安全。
- `abort`：仅 Disable/应用退出，幂等进入模块安全待机并释放资源。
- `read_status`：只读实际状态；Run 开始前保存为快照。
- `manual_action`：Idle 时执行 Test/Read/Measure Now，不写实验 DAT。

框架接受同步方法或返回 awaitable 的方法。返回值必须是 JSON object 或 None；禁止
NaN/Infinity/bytes/自定义对象。一次 IPC 消息最大 1 MiB。每次 `measure` 可返回一个
非空 Mapping，或调用一次 `emit_row` 后返回 None。缺行、多行以及 emit 后又 return
都会成为 Error。

### 4.4 逻辑槽位、数据行与事件

```python
def measurement_slots(self, context):
    return [slot for slot in (1, 2, 3, 4) if self.enabled(slot)]

def measure(self, context):
    slot = context.measurement_step.logical_slot
    context.interruptible_sleep(0.1)
    live_system = context.sample_system()
    raw = self.read_trace(slot)
    context.emit_row(
        {f"R{slot}": self.reduce_trace(raw), "StatusCode": 0},
        raw_values=raw,
    )
```

核心取所有 `aligned_slots` 计划的并集。每个槽位对应一个通道行：同槽位参与模块并行，
结果在全部收束后合到该行；未启用该槽位的扫描模块留空，`once_per_slot` 模块每行重新
测量。于是 CH1–CH4 始终写四行，不会合成一行。模块中的 pause/dwell/settle 等等待必须
使用 `context.interruptible_sleep()`，不能直接使用 `time.sleep()`；前者会在 SEQ Pause
时冻结计时，并在 Stop/Error 时协作退出。仪表驱动自身仍必须设置较短、有限的 I/O
超时，因为正在阻塞的驱动调用只能在驱动超时后响应 Stop。

`raw_values` 是可选的有限数值序列：最多 32,768 点，不能含 bool、文本、NaN 或
Infinity，且整条 IPC 仍不能超过 1 MiB。核心按正式 DAT + 模块写入无表头 `rawdata`
sidecar；每个模块在当前槽位的正式结果与自己的原始行顺序绑定。承诺每个正式行都有 rawdata 的模块在
没有有效数值时应发送空序列作为空行占位；完全不使用 rawdata 的模块不要发送空序列。
模块不得把通道名、时间戳或状态混入原始序列，也不得自行写文件。

```python
context.update_status({"Output": "On"})
context.warning("R1 overloaded", "OVER_RANGE", "R1")
context.resolve_warning("OVER_RANGE", "R1")
context.error("Source interlock opened", "INTERLOCK_OPEN", "source")
```

单点非数字/非有限值、超量程、compliance、样本数不足和单通道统计失败属于测量数据
问题：模块丢弃不可写值，写本模块定义的非负整数 `StatusCode`，调用
`context.warning()` 后继续。`0` 固定表示正常；其他数值没有框架统一含义，必须在模块
README 和测试中逐项定义，不能写 `NORMAL`、`ERROR` 等文字。worker/IPC 故障、通信
耗尽、身份不符、协议状态不明、设置读回不一致、未知
路由、触发状态不确定或安全状态无法确认属于系统问题，必须 Error 并中止 SEQ。若数据
异常已经使当前通道、报文边界、路由或输出状态无法判断，也必须升级为系统 Error。

任何非零状态码都不得保留其对应结果组的正式电阻/电压/相位/统计结果，未测组同样
留空；同一宽表中的其他正常组可保留。只可额外保留仍可信且已记录语义的通道、温场、
设定值、样本数或 rawdata。有效数据只是需要提醒时应使用状态码 0 加独立 Warning。

`context.system` 是本次生命周期调用开始时的只读快照。需要在较长测量过程中捕获真实
的新时间点时，调用 `context.sample_system()`；不得把初始快照重复使用后伪装成多次
取样。每个设备项包含 kind、role、control_enabled、连接状态、current、target、rate
和 activity。`context.operation_timeout_seconds` 给出本次生命周期调用的总超时；
initialize、Apply、begin、Measure、end 分别计时。模块应在 Apply 时验证未来每一种
调用分别能在上限内完成，不应把互相独立调用的时长机械相加。

### 4.5 Frontend

Frontend 继承 `ModuleFrontend`，实现 Settings/Status 页面、settings round-trip 和状态
更新。必须遵守：

- Settings 是默认页；变化时发 `settingsChanged`。
- `load_settings()` 只更新控件，不发送仪表命令。
- `Apply Settings` 只由框架放在 Settings 页。
- `set_sequence_running(True)` 禁用全部 backend I/O 按钮，包括资源/Status Refresh、
  Test、Read、Measure 和安全动作；纯本地界面重绘可以保留。
- 不打开后台线程或仪表连接，不写 DAT。
- 用户不能直接关闭模块窗口；Disable 成功后框架隐藏。

## 5. 测试门槛

每个正式扩展在自己的仓库独立测试，之后再运行核心完整测试。

Device Plugin 至少覆盖：

1. 清单/API/core、框架共享依赖范围和额外依赖 lock；
2. connect 身份不匹配、协议超时和幂等 disconnect；
3. poll 新鲜时间戳、单位、target/rate/activity；
4. 上下限、速率、Hold 新鲜读回；
5. 读链路失联恢复和 60 秒故障；
6. 写超时不重放；
7. Stop/Error/强杀进程后的句柄释放。

Measurement Module 至少覆盖：

1. manifest、固定列和 Settings round-trip；
2. initialize 不 Apply、Apply 确认、完整生命周期；
3. 显式 mode、槽位计划与并集、每通道一行、同槽位模块并行，以及 `once_per_slot`
   在每行重新测量；
4. 测量数据 Warning 写模块自有整数状态码并继续、系统 Error 终止，以及 Warning
   去重/恢复；
5. 未知列、NaN/Infinity、非 JSON 和超大消息；
6. 启动/操作/关闭超时及强制回收；
7. 框架依赖无需安装、额外 wheel 精确离线安装、篡改和隔离；
8. 1080p/4K Frontend 布局、窗口重建与信号连接。

真实硬件测试按“只读身份 → 最低风险输出 → Stop/Error/Hold → 断线恢复 → 长时间运行”
逐级进行。软件测试通过不代表可以跳过仪表自身限值、硬件互锁和人工急停。

## 6. 发布

- 提升扩展 version；保持稳定 ID。
- 只为框架未提供的额外依赖锁定全部传递项并生成目标平台 wheels/hash。
- 不提交地址密码、令牌、私钥、`plugin_runtime`、`plugin_state`、`module_data` 或 DAT。
- 在干净离线 Windows 环境执行手动复制、首次信任；仅有额外依赖时执行
  Install Dependencies，然后重启并验证完整生命周期。
- 记录支持的 OpenLab Control 范围、接线、仪表面板前置设置、安全上限、恢复流程和
  已测试固件。
