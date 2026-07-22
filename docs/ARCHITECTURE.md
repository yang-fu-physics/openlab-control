# 系统架构

本文描述 OpenLab Control 0.10.0 的实际实现边界。设备控制与测量模块是两套不同的扩展机制，不能混用。

## 总览

```text
PySide6 主进程 / GUI 线程
├─ MainWindow、SEQ Editor、Data Browser
├─ Modules Manager
├─ 每个模块的自定义 Settings / Status 前端
└─ 只通过 RuntimeService 提交操作
             │
             ▼
同一主进程 / OpenLabRuntime 后台线程 / asyncio
├─ DeviceManager：温度、磁场、Monitor
├─ SequenceEngine：嵌套 SEQ、Pause、Stop、Error
├─ MeasurementModuleService：模块生命周期与并行 Measure
├─ DatRunLogger：唯一 DAT / events.dat 写入者
└─ EventManager：Warning / Error 锁存与弹窗去重
             │ IPC（每个模块一条串行连接）
             ├───────────────┬───────────────┐
             ▼               ▼               ▼
模块 A 独立进程      模块 B 独立进程      模块 N 独立进程
完整仪表组合与流程    完整仪表组合与流程    完整仪表组合与流程
```

关键边界：

- GUI 对象只存在于 GUI 线程。
- 温度、磁场和 Monitor 驱动只存在于 Runtime 线程。
- 测量仪表通信只存在于各自模块工作进程；模块前端不得直接打开 VISA、串口或厂商 SDK。
- 模块只获得系统状态的只读副本，没有设置温度或磁场的 API。
- 实验 DAT 只能由 `DatRunLogger` 写入，模块进程无文件写权限接口。

## 目录与职责

| 路径 | 职责 |
|---|---|
| `src/labcontrol/runtime.py` | GUI 与异步运行时之间的线程安全入口 |
| `src/labcontrol/plugins.py` | 控制/Monitor 设备实例、锁、轮询、限制与判稳 |
| `src/labcontrol/sequence/engine.py` | SEQ 状态机、嵌套 Scan、Measure 与结束原因 |
| `src/labcontrol/datafile.py` | 运行目录、固定列、流式行与事件文件 |
| `src/labcontrol/measurement/api.py` | 模块后台公开契约和只读 Context |
| `src/labcontrol/measurement/frontend_api.py` | 模块前端公开契约 |
| `src/labcontrol/measurement/manifest.py` | `module.toml`、发现、API/依赖验证、源码加载 |
| `src/labcontrol/measurement/worker.py` | 独立进程启动、IPC 请求/响应和事件流 |
| `src/labcontrol/measurement/service.py` | Enable/Disable、并行 Measure、Schema 验证和事件映射 |
| `src/labcontrol/ui/measurement_modules.py` | Modules Manager 和模块独立窗口 |
| `modules/` | 用户可维护的测量模块源码目录 |
| `module_data/<id>/` | 自动保存的模块 Settings |
| `module_runtime/site-packages/` | 所有模块共享的第三方依赖 |

## 设备能力模型

设备配置仅允许三类：

- `temperature`：可 Set/Hold，参加中央判稳。
- `field`：可 Set/Hold，参加中央判稳。
- `monitor`：只读单值，不接受 Target/Hold，不作为标准温度或磁场。

旧 `measurement` 设备类型已删除。测量仪表必须放入 Measurement Module；因此底部只显示 Temperature、Magnetic Field 和 `2nd Stage` 等控制/Monitor 状态块，不显示 Transport 状态块。

每个设备有独立 `asyncio.Lock`。同一设备的 Poll、Set 和 Hold 串行执行，不同设备可以并发轮询。数值判稳由中央 `StabilityEvaluator` 完成，不依赖厂商的文字状态。

## 模块发现与信任边界

启动时扫描配置指定的 `modules/` 下所有含 `module.toml` 的一级子目录。清单在任何源码导入前验证：

- ID 格式、唯一性；
- API 版本；
- Python frontend/backend 入口格式；
- 固定且唯一的数据列；
- 依赖语法与模块间版本范围冲突；
- 当前共享环境是否满足依赖。

模块源码是受信任的本地代码，不是安全沙箱。工作进程隔离的目的，是隔离阻塞通信、仪表状态和崩溃影响，不是抵御恶意代码。Frontend 仍运行在主进程，因此只应安装经过审查的模块。

## 模块进程与 IPC

Enable 时创建一个 `spawn` 工作进程。工作进程加载 backend 类后发送 Ready；随后同一模块的请求严格串行：

```text
initialize → apply_settings（可选，多次）
           → begin_sequence → measure（可多次） → end_sequence
           → abort（只在 Disable 或应用退出） → close worker
```

IPC 消息分为：

- Request/Response：一次生命周期调用及其最终结果；
- `row`：一行测量值，可在一次 Measure 中发送多次；
- `status`：更新模块 Status 页面；
- `warning`：可恢复测量告警。

框架不在生命周期外层增加仪表通信超时。每个真实驱动必须给 VISA/串口/TCP/厂商 SDK 设置有限超时，确保生命周期最终返回。应用整体关闭仍有运行时退出保护，但它不是仪表通信策略。

## Frontend 模型

模块提供一个 `ModuleFrontend` 子类，自行创建 Settings 和 Status 内容。框架只固定：

- 两个页签，Settings 在前且默认显示；
- `Apply Settings` 按钮与确认；
- SEQ 期间 Settings 只读、Apply 和手动动作禁用；
- 窗口属于主窗口、可移动/最小化、保持在主窗口之前，但不全局置顶；
- 用户不能直接关闭，Disable 成功后由框架隐藏；
- 主窗口最小化时，当前可见模块窗口一起最小化。

Frontend 通过 `ModuleFrontendContext` 只能请求手动动作或状态刷新。它没有设备控制对象，也不能直接写 DAT。

## SEQ Measure 数据流

```text
SEQ 到达 T Measure
  ├─ 运行锁定的模块集合为空
  │    └─ Warning + 一行系统快照，继续 SEQ
  └─ 对全部 Enabled 模块同时发送 measure
       ├─ 模块 A emit R1
       │    └─ 捕获此刻温度/场/Monitor → 立即写一行
       ├─ 模块 B emit Voltage
       │    └─ 捕获此刻温度/场/Monitor → 立即写一行
       ├─ 模块 A emit R2 ...
       └─ 等所有模块完成
            ├─ 无 Error：继续下一条 SEQ
            └─ 有 Error：结束原因为 error
```

每个模块内部行顺序保持；多个模块之间按中央运行时收到结果的顺序串行写盘。一次 Measure 返回多行时，每行都有独立的实时系统快照。模块返回映射也可形成一行，但推荐用 `emit_row()` 流式发送。

## Schema 锁定

Run 开始时固定：

- Enabled 模块集合；
- 每个模块 `module.toml` 中声明的列和单位；
- 各模块本次保存的 desired Settings；
- 模块实际 Status 快照。

列名自动变为 `<module_id>.<column>(<unit>)`。模块发出未声明列或不可序列化值时产生 Error，并中止 SEQ。SEQ 运行期间禁止 Enable、Disable、Refresh、Apply 和手动动作，因此本次 DAT 的列不会改变。

## 生命周期失败语义

| 阶段 | 失败后的行为 |
|---|---|
| `initialize` | Enable 失败，仍 Disabled，工作进程关闭，Error |
| `apply_settings` | 保留 Enabled 与窗口；设置不标为 Applied，Error |
| `begin_sequence` | SEQ Faulted；进入 `end_sequence("error")`，不 Abort |
| `measure` Warning | 记录 Warning，保留有效行，等待其余模块，继续 SEQ |
| `measure` Error | 等并发调用收束，SEQ Faulted，调用 `end_sequence("error")`，不 Abort |
| `end_sequence` | 最终状态强制 Faulted；模块保持 Enabled、窗口显示 Status，不自动 Abort |
| Disable 的 `abort` | Disable 失败；仍 Enabled，窗口保持打开并切到故障状态 |
| 应用退出的 `abort` | 先保存 Settings，再尝试 Abort 和关闭工作进程 |

Stop 或 Error 后温度与磁场执行配置的 Hold 策略；模块则只执行 `end_sequence(reason)`。`abort()` 不用于普通 SEQ 结束。

## Warning 与 Error 传播

所有事件都进入 `EventManager`。活动键为 `source + code + context`：

- 同一活动键反复报告只增加计数，不重复弹窗；
- `resolve()` 后同一事件再次发生才重新弹窗；
- Warning 不改变 SEQ 状态；
- Error 在 Running/Paused 时请求 fatal Stop；
- 事件同步写 `events.dat`，GUI Run Log 也显示。

模块自身应使用稳定的 code/context，例如超量程可用 code=`OVER_RANGE`、context=`R1`，避免每次测量创建不同键。

## 运行目录一致性

Run 时先保存模块前端的当前 Settings，然后后台读取实际 Status，再创建：

```text
runs/<timestamp>_<sequence>/
├─ sequence.seq
├─ configuration.toml
├─ module_settings/
│  ├─ <id>.settings.toml
│  └─ <id>.status-at-start.json
├─ experiment.dat
└─ events.dat
```

只保存本次 Enabled 模块。Settings 文件代表期望/界面值；Status JSON 代表运行开始时后台读取到的实际状态，两者不能互相替代。

## 已知边界

- 0.10.0 只实现 Python 源码 backend；清单保留 `backend_type`，但 executable backend 尚未实现。
- 模块前端运行在 GUI 进程，错误的前端代码仍可能影响界面。
- 依赖安装当前由界面同步执行，安装期间窗口可能短暂无响应；实际测量不受此路径影响。
- 首版不做模块热替换。Refresh 仅在 SEQ Idle 且所有模块 Disabled 时允许。
