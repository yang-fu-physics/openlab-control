# 系统架构

本文描述 OpenLab Control 0.19.0 的当前边界。

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
├─ InstrumentManager：控制许可、限制、恢复和快照
├─ SequenceEngine：SEQ、Pause、Stop 和 Error
├─ MeasurementModuleService：模块调度与结果校验
├─ DatRunLogger：所有运行文件的唯一写入者
└─ EventManager / AlarmReporter
            │ 受限 JSON IPC
            ├─ 每个仪表实例一个 spawn 子进程
            └─ 每个 Enabled 模块一个 spawn 子进程
```

GUI 不直接操作仪表。每个模块内部请求串行，不同模块可并行。IPC 不使用 pickle；单条
消息限制为 1 MiB，并拒绝 NaN、Infinity、复杂对象和未知 DAT 列。

进程隔离限制阻塞和崩溃影响，但不是恶意代码沙箱。模块 Frontend 仍在 GUI 进程，
因此只应把已审查的本地代码放进 `modules/` 或 `system_instruments/`。

## 两套接入边界

System Instrument 与 Measurement Module 分开：

- System Instrument 提供温度、磁场或 Monitor；核心负责控制许可、上下限、速率、失联恢复和
  已注册事件响应的安全调度。
- Measurement Module 拥有完成一次测量所需的仪表、切换器、时序、状态码和安全动作；
  它只能读取温场快照，不能控制温度或磁场。

模块目录最小为 `module.toml + backend.py`。清单只含名称和版本；DAT 列来自
`Module.columns`。发现阶段不导入模块源码，Enable 后才在独立工作进程中加载后台。

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 是框架共享依赖。仪表或模块
需要新包时更新核心依赖并重新构建发布包，不在运行中的程序里建立第二套 Python 环境。

System Instrument 的作者接口是同步的
`open/read_status/read_measurement/set_target/hold/execute_sequence_command/event_responses/close`。
驱动只返回包含 `value`、可选 `auxiliary` 和控制状态的普通字典。单控制端点可在顶层返回
`target/rate/moving/ready`；多个不同控制端点必须返回按 control ID 分组的 `controls`。
核心统一生成时间戳、连接状态和内部快照，并按 Controller 面板独立判稳。

配置边界也分层：

```text
configs/general.toml
        通用应用、日志、报警和进程参数

system_instruments/<id>/instrument.toml
        API v4 作者模板：config_fields / controls / panels / readings / sequence_commands
                    │ Instrument Scanner
                    ▼
configs/instruments/<id>.toml
        一个或多个物理实例，只引用固定 panel ID 并保存现场选择与限制

configs/visa.resources.toml
        未分配给 System Instrument 的 VISA，只提供给 Measurement Module
```

扫描器把作者清单中除 `panels` 模板外的静态元数据复制到生成文件，并写入
`[[instances]]`。每个物理实例只创建一个 worker 和通讯会话；多个面板与控制端点复用它。
System 实例选中的 VISA 地址不会进入 Measurement 资源清单。专用网络仪表由自己的
`config_fields` 提供 `host`、`port` 等连接值。

实例面板的 `role` 只有 `none`、`sample_temp` 和 `field`。后两者各自全局最多一个，只能
用于支持对应 kind 的 controller；`none` 可重复。没有任何生成 System 面板时配置仍有效，
主程序可以启动。三个内置仿真也必须由操作者明确启用。

扫描器保存的是生成配置全集：选中的文件原子覆盖，不在最终预览中的生成文件删除。PID
文件是例外；第一次从作者示例或操作者选择的来源复制后，扫描器不再覆盖或删除。

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

`ModuleAPI` 仅提供可中断等待/`checkpoint()`、只读仪表快照、Warning、状态更新和本次
总 timeout。`api.instruments()` 会触发一次测量专用即时仪表采样，与前面板常规轮询分开；
并发模块请求合并为一次采样。System Instrument 未实现 `read_measurement()` 时自动使用完整
`read_status()`。Pause 冻结 `api.sleep()` 计时；Stop 在检查点取消调用。任意厂商阻塞 I/O 仍
必须由模块设置有限 timeout。

## SEQ 与安全收尾

SEQ 解析为树；Scan 可任意嵌套，Call Sequence 在预检时展开并检查递归。Pause、正常完成、
Stop、Error 和任务取消都不向 System Instrument 发送 Set 或 Hold。System Instrument 可以
注册纯数据事件响应；核心负责匹配、最高优先级执行、锁存和人工复位，仪表代码不能直接取得
另一台仪表对象。

模块在 `run_end` 中按自身协议完成一次 Run 的输出关闭或状态保持，`close` 只在 Disable
和应用退出执行。若 `run_end` 或 `close` 失败，核心报告 Error 并有界回收 worker；进程
被终止不等于外部仪表已安全。

读链路失联时，InstrumentManager 关闭旧 worker 并按配置重连；默认恢复窗为 60 秒。恢复后
读取实际 target/rate，不自动重放写命令。写命令 timeout 属于“可能已经执行”的歧义
状态，立即 Faulted，不自动重试。

## 数据与事件

一次 Run 固定保存：

```text
runs/<timestamp>_<sequence>/
├─ sequence.seq
├─ configuration/
│  ├─ general.toml
│  ├─ visa.resources.toml       # 原文件存在时
│  ├─ instruments/*.toml
│  └─ pid/*.toml
├─ module_settings/*.settings.toml
├─ module_settings/*.status-at-start.json
├─ rawdata/*.rawdata
├─ experiment.dat
├─ instrument_status.dat
└─ events.dat
```

`DatRunLogger` 是唯一写入者。每条测量行写入前由核心取得测量专用即时温场快照；若模块刚在
0.1 秒内读取则复用该样本。常规仪表轮询
另行节流写入 `instrument_status.dat`。同一物理仪表的辅助读数随主快照使用一个连接，并在
Run 开始时冻结为固定列。Data Browser 只跟踪用户打开的 DAT，不与当前 Run 绑定。

空闲时仪表与前面板按 `poll_interval_seconds` 采样；SEQ 控制期间用较短的
`control_poll_interval_seconds` 做判稳，但发给前面板和 Live Trend 的快照仍按前者节流。
Measurement Module 的即时读取独立于这两个周期。

同一台物理仪表的 `open`、写控制、Hold、测量读取、后台轮询和 `close` 均经过同一个优先
串行入口，不会同时访问仪表。入口不抢占已经开始的后台调用：当前完整仪表事务返回或超时
后，控制与安全操作先执行，等待中的 `read_measurement()` 再先于后台 `read_status()`。因此后台
状态刷新不会把测量用即时读数长期压在队尾。

事件键为 `source + code + context`。重复活动 Warning/Error 只增加 Count；resolve 后才可
再次弹窗。Warning 继续运行，Error 在 Running/Paused 时请求 fatal Stop。报警 HTTP
失败只形成本地 Warning，不参与仪表安全动作。

## 真实仪表边界

OpenLab Control 0.19.0 尚未完成真实仪表验证。软件进程隔离不能替代仪表限流、限压、
限温、磁体保护、硬件互锁或人工急停。接入真机前必须保留并通过模块自己的命令顺序、
读回、量程、timeout、异常清理与协议解析测试，再进行低风险现场验证。
