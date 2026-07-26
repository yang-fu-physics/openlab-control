# 系统架构

本文描述 OpenLab Control 0.11.1 的实际实现边界。Device Plugin 与 Measurement Module
是两套不同的扩展机制；它们分别放在独立共享仓库中，不进入核心源码。

## 进程与线程模型

```text
PySide6 主进程 / GUI 线程
├─ MainWindow、SEQ Editor、Data Browser
├─ Modules Manager
├─ Measurement Module Frontend（Settings / Status）
└─ RuntimeService 线程安全入口
              │
              ▼
主进程 / OpenLabRuntime 后台线程 / asyncio
├─ DeviceManager：角色、限制、恢复与快照
├─ SequenceEngine：嵌套 SEQ、Pause、Stop、Error
├─ MeasurementModuleService：模块生命周期与并行 Measure
├─ DatRunLogger：DAT / events.dat 唯一写入者
├─ EventManager：Warning / Error 锁存与弹窗去重
└─ AlarmReporter：有界队列中的异步 HTTP Warning/Error 报告
       │ JSON IPC（每实例独立连接，单消息 ≤ 1 MiB）
       ├──────────────┬──────────────┬──────────────┐
       ▼              ▼              ▼              ▼
温度设备进程      磁场设备进程     Monitor 进程    模块工作进程（每模块一个）
```

关键边界：

- GUI 对象只存在于 GUI 线程；模块 Frontend 不得打开仪表连接。
- 每个配置设备实例独占一个 `spawn` 子进程，阻塞/崩溃不会占住其他设备。
- 每个 Enabled 模块独占一个 `spawn` 子进程并拥有其测量仪表。
- 框架依赖由核心统一锁定和加载；只有扩展额外依赖才插入对应子进程的 `sys.path`，
  不污染核心/GUI，也不执行 `.pth`。
- 模块只获得系统状态的 JSON 只读副本，没有设置温度或磁场的 API；长测量可请求新快照，
  并通过协作检查点响应 SEQ Pause/Stop。
- 报警发射线程不参与 SEQ 判定或仪表安全动作；网络失败只锁存本地 Warning。
- `DatRunLogger` 是 DAT 和事件文件的唯一写入者。
- 子进程隔离用于约束阻塞、资源和崩溃影响，不是恶意代码沙箱；Frontend 仍在主进程，
  所以扩展必须经过人工审查与内容指纹信任。

## 扩展仓库与安装边界

```text
OpenLab Control core repository
├─ modules/                    手动安装目标，默认空
├─ device_plugins/             手动安装目标，默认空
├─ plugin_runtime/             生成内容，不提交
├─ plugin_state/               本机信任，不提交
└─ plugin_templates/
   ├─ measurement-modules-repository/      公共/共享仓库模板
   └─ device-plugins-private-repository/   私密/共享仓库模板
```

一个模块或设备插件目录包含清单、源码、可选 `requirements.lock` 和可选 `wheels/`。
发现阶段在导入源码前验证：

- ID、版本、API、`core_requires` 和入口文件；
- 支持的设备 kind 或固定测量列；
- 依赖语法、框架共享版本兼容性，以及额外依赖的精确带哈希 lock 和禁止 URL/安装选项；
- 目录树不能含逃逸链接或不安全内容，并计算 SHA-256 内容指纹。

首次加载必须信任准确的 `type + id + version + fingerprint`。源文件、清单、lock 或 wheel
发生变化后，旧信任不再匹配。

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 由主框架提供统一版本。
manifest 中兼容的声明不生成私有 runtime；不兼容声明在源码导入前拒绝。只有框架没有
提供的额外依赖才从本地 wheel 安装，命令固定使用 `--no-index --only-binary=:all:
--require-hashes`。安装先进入 staging 目录，验证后原子替换。运行前再次检查扩展指纹、
额外依赖版本、runtime marker 和整个额外依赖树摘要。

## 设备能力与角色

设备 kind：

- `temperature`：可作为主控或次要读数；只有 `control_enabled = true` 才暴露 Set/Hold。
- `field`：同上。
- `monitor`：只读单值，必须 `role = "monitor"` 且不能启用控制。

角色：

- 每个 temperature/field kind 最多一个 `role = "primary"`；SEQ 缺少对应主设备或新鲜
  读回时拒绝 Run。
- primary 必须 `control_enabled = true`。
- 未显式写 role 的新增 temperature/field 设备默认 secondary 且不可控；旧单设备配置
  仍兼容地把该种类第一个设备提升为 primary。
- 手动控制和 SEQ 最终都经过 `DeviceManager` 的上下限、最大速率、角色和连接状态检查。

设备插件自身实现 connect/poll/set/hold/disconnect 和厂商协议。核心配置决定安全包络；
插件还应重复验证仪表特有限制。仪表面板的默认设置目前不由框架自动应用。

## 设备连接恢复

正常状态为 Starting → Connected。读操作失败时：

1. 状态变为 Reconnecting，关闭/终止旧工作进程。
2. 按 `device_reconnect_interval_seconds` 创建新进程、连接并取得新鲜快照。
3. 对可控设备核对恢复后的 target/rate 与最后确认状态；不自动重放写命令。
4. 成功后回到 Connected；超过 `device_reconnect_timeout_seconds`（默认 60 秒）则
   Faulted。

SEQ 遇到主设备 Reconnecting 时暂停推进，并冻结 Wait/Settle 等活动计时；恢复后继续。
达到恢复最终上限或状态核对失败产生 Error 并走 Hold/Fault 路径。写操作超时具有歧义，
不会重试或重放，而是立即 Faulted，防止同一硬件命令执行两次。

## SEQ、Pause、Stop 与状态

SEQ 解析器生成树形文档，Scan 可任意嵌套，Call Sequence 在预检/进度中展开并检查递归。
Pause 只在安全检查点停止调度并冻结可中断计时，不主动改变输出。Stop、Error、取消和
应用退出都会尝试对所有可控温场设备执行 Hold Current；保持命令必须基于新鲜当前读回。
若 Hold 无法确认，最终状态为 Faulted，不能假装安全停止。

## Measurement Module 生命周期

每次启动所有模块 Disabled。Enable 前重新验证内容、依赖 runtime 和信任，然后创建工作
进程：

```text
initialize(saved settings; do not apply)
→ apply_settings（仅用户在 Settings 页确认，可多次）
→ begin_sequence
→ measure（可多次、一次可 emit 多行）
→ end_sequence(completed | stopped | error)
→ abort（仅 Disable 或应用退出）
→ close/force-stop worker
```

框架分别限制启动、单次操作和关闭总时间。请求包含等待同模块前一请求锁的时间；超时后
连接失效并有界 terminate/kill。应用退出时各模块并行清理，避免总时长随模块数线性增加。

一个 `Measure` 同时请求所有本次 Enabled 模块，并等待全部收束。模块事件：

- `row`：固定 Schema 的一行，可发送多次；
- `status`：更新 Status 页面；
- `warning` / `resolve`：锁存或解除可恢复事件；
- Response：生命周期调用最终结果。

IPC 使用受大小限制的 UTF-8 JSON，不使用 pickle。NaN、Infinity、复杂对象、未知列和
超大消息都会被拒绝。一个模块内事件保持发送顺序；不同模块到达中央后由单一 logger
串行写盘。

## DAT、快照和 Data Browser

Run 开始固定 Enabled 模块集合及列，保存：

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

动态列只发生在 Run 开始：系统列 + 每个模块清单列，名称带 `<module_id>.` 前缀。一次
Measure 的每个模块行都附带当时温度、磁场和 Monitor 快照。写盘可每行 Flush。

Data Browser 与当前 Run 不绑定，只跟踪用户明确打开的 DAT。定时器检查文件大小/修改
时间并增量刷新；对应 `.plt` 保存显示设置，不改变 DAT。

## Warning / Error

活动事件键为 `source + code + context`：

- 同一活动键重复只增加 Count，不重复弹窗；
- resolve 后再次发生才重新弹；
- Warning 继续 SEQ；
- Error 在 Running/Paused 时请求 fatal Stop；
- 事件 Raised/Repeated/Resolved 都写 `events.dat`。

## 已知边界

- 当前只实现 Python 源码扩展，不能抵御恶意插件。
- 模块 Frontend 在 GUI 进程，错误 UI 代码仍可能影响主界面。
- 依赖安装在 GUI 操作中同步执行，可能短暂停止界面响应，但不在运行中允许执行。
- 不支持扩展热替换；Refresh 要求 SEQ Idle 且相关模块 Disabled。
- 本 Beta 尚未经过真实温控仪、磁体电源或测量仪表验证。
