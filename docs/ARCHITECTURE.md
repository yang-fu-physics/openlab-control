# 系统架构

本文描述 OpenLab Control 0.13.1 的当前边界。

## 运行模型

```text
GUI 主线程
├─ MainWindow / SEQ Editor / Data Browser
├─ Modules Manager
├─ 模块 Frontend（普通 QWidget）
└─ RuntimeService 线程安全入口
            │
            ▼
后台 asyncio 线程
├─ DeviceManager：设备角色、限制、恢复和快照
├─ SequenceEngine：SEQ、Pause、Stop 和 Error
├─ MeasurementModuleService：模块调度与结果校验
├─ DatRunLogger：所有运行文件的唯一写入者
└─ EventManager / AlarmReporter
            │ 受限 JSON IPC
            ├─ 每个设备实例一个 spawn 子进程
            └─ 每个 Enabled 模块一个 spawn 子进程
```

GUI 不直接操作仪表。每个模块内部请求串行，不同模块可并行。IPC 不使用 pickle；单条
消息限制为 1 MiB，并拒绝 NaN、Infinity、复杂对象和未知 DAT 列。

进程隔离限制阻塞、崩溃和依赖影响，但不是恶意代码沙箱。模块 Frontend 仍在 GUI
进程，因此首次加载必须核对来源、版本、路径和内容指纹。

## 扩展边界

Device Plugin 与 Measurement Module 分开：

- Device Plugin 提供温度、磁场或 Monitor；核心负责角色、上下限、速率、失联恢复和
  Hold 策略。
- Measurement Module 拥有完成一次测量所需的仪表、切换器、时序、状态码和安全动作；
  它只能读取温场快照，不能控制温度或磁场。

模块目录最小为 `module.toml + backend.py`。清单只含名称、版本和可选额外依赖；DAT 列
来自 `Module.columns`。发现阶段不导入模块源码，Enable 前再次验证完整目录指纹和依赖。

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 是框架共享依赖。只有额外
依赖进入 `plugin_runtime/<type>/<id>/<fingerprint>/site-packages`；离线安装固定使用
`--no-index --only-binary=:all: --require-hashes`，并验证 `requirements.lock`、wheel、
runtime marker 和依赖树摘要。

## Measurement Module 协议

作者只需实现：

```text
open(api)
measure(slot, api) -> row | (row, raw_values)
close(api)
```

可选：

```text
configure(settings, api)
on_event(event, data, api)
slots = N | sequence[int] | property
sequence_commands = sequence[dict]
execute_sequence_command(command_id, parameters, api)
frontend.py: Frontend(QWidget) with load()/dump()
```

核心调用顺序：

```text
Enable:  spawn worker → 验证可选指令声明 → open → 向 UI 注册指令
Apply:   configure
Run:     on_event("status") → on_event("run_start") → read slots
Measure: 对每个逻辑槽位并行调用参与模块的 measure
Finish:  on_event("run_end", reason)
Disable: close → shutdown worker
```

模块指令元数据只由隔离 worker 读取并转换为受限 JSON；主进程不会为发现菜单而提前导入
`backend.py`。只有 `open` 完成后的 Enabled 消息携带注册表，Frontend 创建成功后才加入
右侧顶层组。Disable、worker timeout 或 IPC 失效会移除注册表。SEQ 文档保存通用模块 ID、
稳定指令 ID 和 JSON 参数，因而模块缺失时仍能解析、标红和原样保存，但执行预检失败。

`Module Scan` 的点列表由核心展开。每点通过同一模块串行 worker 调用
`execute_sequence_command`，成功后才递归执行子树；指令自身不进入 DAT。模块 Warning
跳过失败点的子树并继续，Error 中止 SEQ。参数弹窗验证、运行时元数据验证和模块后端的
真实仪表验证是三层不同边界，核心不提供任意 SCPI 直通指令。

保存设置或加载 SEQ 伴随设置只更新 Frontend，不会自动 Enable 或 configure。Run 开始时
冻结 Enabled 模块、列和槽位。声明 `slots` 的模块只参与这些槽位；未声明的模块跟随所有
槽位。若没有模块声明槽位，唯一槽位是 1。

同一槽位的模块结果合并为一行；扫描模块未参与的列为空。`measure` 只返回一行，不存在
流式 `emit_row`。模块可额外返回最多 32,768 个有限 rawdata 数值，由核心写入模块独立
sidecar。

`ModuleAPI` 仅提供可中断等待/`checkpoint()`、只读设备快照、Warning、状态更新和本次
总 timeout。Pause 冻结 `api.sleep()` 计时；Stop 在检查点取消调用。任意厂商阻塞 I/O
仍必须由模块设置有限 timeout。

## SEQ 与安全收尾

SEQ 解析为树；Scan 可任意嵌套，Call Sequence 在预检时展开并检查递归。Pause 不主动
改变输出。Stop、Error、任务取消和应用退出都会让可控温场设备尝试 Hold Current；Hold
必须基于新鲜读回，无法确认时最终状态为 Faulted。

模块在 `run_end` 中按自身协议完成一次 Run 的输出关闭或状态保持，`close` 只在 Disable
和应用退出执行。若 `run_end` 或 `close` 失败，核心报告 Error 并有界回收 worker；进程
被终止不等于外部仪表已安全。

读链路失联时，DeviceManager 关闭旧 worker 并按配置重连；默认恢复窗为 60 秒。恢复后
读取实际 target/rate，不自动重放写命令。写命令 timeout 属于“可能已经执行”的歧义
状态，立即 Faulted，不自动重试。

## 数据与事件

一次 Run 固定保存：

```text
runs/<timestamp>_<sequence>/
├─ sequence.seq
├─ configuration.toml
├─ module_settings/*.settings.toml
├─ module_settings/*.status-at-start.json
├─ rawdata/*.rawdata
├─ experiment.dat
├─ device_status.dat
└─ events.dat
```

`DatRunLogger` 是唯一写入者。每条测量行只采一份温场快照；设备轮询另行节流写入
`device_status.dat`。Data Browser 只跟踪用户打开的 DAT，不与当前 Run 绑定。

事件键为 `source + code + context`。重复活动 Warning/Error 只增加 Count；resolve 后才可
再次弹窗。Warning 继续运行，Error 在 Running/Paused 时请求 fatal Stop。报警 HTTP
失败只形成本地 Warning，不参与仪表安全动作。

## 真实仪表边界

OpenLab Control 0.13.1 尚未完成真实仪表验证。软件进程隔离不能替代设备限流、限压、
限温、磁体保护、硬件互锁或人工急停。接入真机前必须保留并通过模块自己的命令顺序、
读回、量程、timeout、异常清理与协议解析测试，再进行低风险现场验证。
