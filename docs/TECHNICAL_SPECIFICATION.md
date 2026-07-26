# OpenLab Control 0.11.1 技术规格

状态：Stable Core（仿真验证；未验证真实仪表）
日期：2026-07-26
作者：yang-fu-physics `<yfu.physics@gmail.com>`

## 1. 目的

构建一个 MultiVu 风格、面向外部低温与磁场实验设备的可扩展桌面框架。系统不控制 PPMS 本体；它统一控制温控仪、磁体电源、Monitor，并通过独立 Measurement Module 编排一台或多台测量仪表。

## 2. 范围

### 2.1 包含

- Python 3.11+ / PySide6 Windows 桌面 UI；
- 配置驱动的温度、磁场、只读 Monitor 插件；
- 中央数值判稳、目标/速率限制、Hold；
- 单行 `.seq` 编辑、任意 Scan 嵌套和执行状态机；
- 源码 Measurement Module 发现、依赖、独立进程和生命周期；
- 并行模块测量与多行流式中央 DAT；
- Warning/Error 锁存、去重弹窗、事件日志；
- 独立 DAT Browser 和 `.plt` 显示配置；
- 内置仿真设备，以及位于独立仓库模板的 `simulated_transport` 示例模块；
- 源码与 Windows 文件夹式发布包。

### 2.2 不包含

- PPMS/MultiVu 本体控制；
- 未经验证的真实仪表驱动；
- 云端账户、远程多用户权限；
- 运行中模块热加载；
- 模块间隐式共享同一物理仪表；
- 0.10.0 中的 executable backend 实现；
- 对不受信任模块源码的安全沙箱。

## 3. 功能需求

### 3.1 主界面

- UI-001：主界面 SHALL 以英文为主。
- UI-002：SEQ 与 Data Browser SHALL 是 MDI 内浮动窗口。
- UI-003：温度/磁场状态块 SHALL 双击打开手动弹窗；正常不显示。
- UI-004：Monitor SHALL 只显示，不提供设置。
- UI-005：温度 SHALL 显示三位小数；Oe SHALL 显示两位小数。
- UI-006：界面 SHALL 支持自动及 0.75–2.0 手动缩放。
- UI-007：长 SEQ/DAT 路径 SHALL 省略显示且不能撑大左 Dock。
- UI-008：临时对话框和 Refresh 后废弃的模块窗口 SHALL 断开并销毁，不得在主窗口子对象树中累积。

### 3.2 SEQ 编辑与执行

- SEQ-001：每个指令 SHALL 占一行，并以 T/F 保存启用状态。
- SEQ-002：命令列表 SHALL 位于右侧；双击 SHALL 打开参数弹窗并插入。
- SEQ-003：已有行双击 SHALL 打开参数弹窗。
- SEQ-004：编辑器 SHALL 支持多行 Disable/Enable/Delete/Copy/Paste 及键盘操作。
- SEQ-005：Scan Temperature、Scan Field、Scan Time SHALL 任意多层嵌套。
- SEQ-006：Scan Temperature SHALL 支持 Linear 与保序/保重复 List。
- SEQ-007：List SHALL 在第一次移动前整表验证。
- SEQ-008：Measure SHALL 只有无参数单行 `T Measure`。
- SEQ-009：旧 Initialize 和带参数 Measure SHALL 产生解析 Error 并阻止 Run。
- SEQ-010：Running/Paused/Stopping SHALL 锁定 SEQ 与模块配置变更。
- SEQ-011：Stop/Error 后温度与磁场 SHALL Hold Current，不得继续追逐旧 Target。
- SEQ-012：进度总数 SHALL 展开 Scan 重复次数和可解析的 Call Sequence，不得在完成前提前达到 100%。
- SEQ-013：手写 SEQ 和运行时 SHALL 使用与参数窗口一致的数值边界；不得静默截断非法 Wait、Scan 点数、持续时间或速率。

### 3.3 设备控制

- DEV-001：Device kind SHALL 仅为 temperature、field、monitor。
- DEV-002：每个配置设备实例 SHALL 在独立 spawn 子进程运行，同一实例请求 SHALL 串行。
- DEV-003：不同设备 SHALL 可并发轮询、恢复和退出。
- DEV-004：Target 与 Rate SHALL 同时在 UI 和运行时由同一配置限制。
- DEV-005：中央 SHALL 使用偏差、窗口斜率、Dwell、Timeout 判稳。
- DEV-006：模块 SHALL 只能获得设备只读快照，不得获得控制引用。
- DEV-007：设备 Connect/Poll/Set/Hold/Disconnect SHALL 有配置化有限框架超时。
- DEV-008：读链路失败后框架 SHALL 终止旧进程，在配置恢复窗内重建连接并验证实际状态；
  写超时 SHALL 不重放并立即 Fault。
- DEV-009：Stop 时任一控制设备未确认 Hold Current，最终运行状态 SHALL 为 Faulted。
- DEV-010：SEQ 参数窗口 SHALL 按类型列出配置设备 ID，使用对应设备的 Target/Rate 限制。
- DEV-011：显式 SEQ 设备 ID SHALL 可保存/重载；省略 ID 或旧角色名 SHALL 解析到该类型
  唯一 primary，而不是目录/配置顺序中的任意设备。
- DEV-012：Settle 判稳与 Sweep 到达目标 SHALL 均有最终等待上限；无新读数时不得无限等待。
- DEV-013：设备 ID SHALL 是非空可打印文本且无首尾空白，并与 UI、SEQ 解析和运行时使用同一精确值。
- DEV-014：每个 temperature/field kind SHALL 最多一个 primary；primary SHALL 可控，
  secondary 默认 SHALL 只读，monitor SHALL 永远不可控。
- DEV-015：SEQ 主设备 Reconnecting 时 SHALL 冻结活动计时；恢复上限后 SHALL Error。
- DEV-016：Run 预检 SHALL 要求所需 primary Connected 且存在新鲜读回。

### 3.4 Measurement Module 发现与依赖

- MOD-001：模块根目录 SHALL 可配置，默认 `modules/`。
- MOD-002：启动和合法 Refresh SHALL 扫描一级子目录 `module.toml`。
- MOD-003：每次应用启动所有模块 SHALL 为 Disabled。
- MOD-004：Manager SHALL 只显示 Enabled、Name、Version 三列。
- MOD-005：Refresh SHALL 仅在 SEQ Idle 且所有模块 Disabled 时允许。
- MOD-006：清单 SHALL 验证唯一 ID、API、入口、backend type、固定列和依赖。
- MOD-007：框架共享依赖范围不兼容，或额外依赖缺失、未哈希、版本不满足/runtime
  完整性失败，SHALL 禁止 Enable。
- MOD-008：PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions SHALL 由核心
  统一锁定并供所有扩展使用；扩展不得用私有副本覆盖框架版本。
- MOD-009：Install Dependencies SHALL 只在扩展存在额外依赖时显示并显式触发；
  Enable 不得自动安装。
- MOD-010：每个 Device/Module 的额外依赖 SHALL 使用按类型、ID 和内容指纹隔离的
  目录，并只注入对应子进程。
- MOD-011：额外依赖安装 SHALL 只使用本地 wheels、精确带 SHA-256 的 lock 和
  `--no-index --require-hashes`；不得在线回退。
- MOD-012：首次加载 SHALL 绑定 type/ID/version/content fingerprint 取得用户信任；
  内容变化 SHALL 使旧信任失效。

### 3.5 模块进程和界面

- PROC-001：每个 Enabled 模块 backend SHALL 在独立 spawn 工作进程运行。
- PROC-002：frontend SHALL 在 GUI 进程/线程运行。
- PROC-003：frontend SHALL 不得直接执行 VISA/Serial/SDK I/O。
- PROC-004：同一模块 IPC 操作 SHALL 串行。
- PROC-005：真实驱动 SHALL 自行配置有限协议超时；框架 SHALL 另行提供可配置的启动、IPC 操作和关闭最终超时。
- PROC-006：模块工作进程超时 SHALL 报告 Error、使该进程失效并在有限时间内回收管道和进程。
- PROC-007：设备和模块 IPC SHALL 使用受大小限制的 JSON，不得反序列化 pickle。
- PROC-008：扩展额外依赖目录 SHALL 在子进程内直接插入，不得处理 `.pth`、覆盖框架
  共享包或污染主进程。
- WIN-001：模块窗口 SHALL 是主窗口拥有的独立 modeless Windows 窗口。
- WIN-002：窗口 SHALL 保持在主窗口之前但不得全局 Always-on-top。
- WIN-003：窗口 SHALL 可移动/最小化，用户不得关闭。
- WIN-004：主窗口最小化 SHALL 最小化当前可见模块窗口。
- WIN-005：Apply Settings SHALL 只属于 Settings 页，Status 页不得显示该按钮。
- WIN-006：模块窗口 SHALL 设置随 UI Scale 缩放的内容安全最小尺寸。
- WIN-007：窗口 SHALL 固定 Settings/Status 两页，默认 Settings；页面内容由模块完全自定义。
- WIN-008：SEQ 期间 Settings SHALL 只读，Apply/手动动作 SHALL 禁用。

### 3.6 模块生命周期

- LIFE-001：Enable SHALL 调用 initialize；成功后才勾选/显示窗口。
- LIFE-002：initialize SHALL 加载保存 Settings 但不得自动应用到仪表。
- LIFE-003：Apply SHALL 明确确认，并调用 apply_settings。
- LIFE-004：Run SHALL 在第一条指令前调用 begin_sequence。
- LIFE-005：每条 Measure SHALL 调用本次锁定模块的 measure。
- LIFE-006：最终 SHALL 调用 end_sequence，reason=`completed|stopped|error`。
- LIFE-007：abort SHALL 只在 Disable 和应用退出调用。
- LIFE-008：Error 停止 SEQ时不得调用 abort。
- LIFE-009：end_sequence 失败 SHALL 使最终状态 Faulted，模块保持 Enabled/可见，不自动 abort。
- LIFE-010：Disable abort 失败 SHALL 报告 Error，并在关闭上限内强制回收工作进程、转为 Disabled；不得把强制回收解释为仪表已安全。

### 3.7 Settings

- SET-001：Settings SHALL 保存于 `module_data/<id>/settings.toml`，与源码分离。
- SET-002：SHALL 在 Apply、Disable、应用关闭和 Run 前保存。
- SET-003：应用关闭 SHALL 先保存，再 abort。
- SET-004：Run 前有未 Apply 修改时 SHALL 提供 Apply and Run、Run Without Applying、Cancel。
- SET-005：Run SHALL 分别保存 desired Settings 和实际 Status。

### 3.8 并行测量与数据

- MEAS-001：一条 Measure SHALL 并行调用所有 Enabled 模块。
- MEAS-002：中央 SHALL 等全部模块完成后才继续 SEQ。
- MEAS-003：模块 MAY 在一次 Measure 中按顺序发出多行。
- MEAS-004：每行到达时 SHALL 捕获最新控制/Monitor 快照并立即写入。
- MEAS-005：同一模块行顺序 SHALL 保持；模块间 SHALL 按中央到达顺序串行写盘。
- MEAS-006：无 Enabled 模块 SHALL Warning、写一行系统快照并继续。
- DATA-001：模块 SHALL 在清单声明固定列/单位。
- DATA-002：列 SHALL 自动加 `<module_id>.` 前缀。
- DATA-003：模块不得直接写实验 DAT。
- DATA-004：未声明列、不支持值类型、NaN 或 Infinity SHALL Error。
- DATA-005：模块 SHALL 自行声明业务 Status/Warning 列；框架不加通用列。
- DATA-006：Warning/Error 时可用温场/Monitor 数据 SHALL 保留。
- DATA-007：默认数据/事件日志 SHALL 使用互不相同的单文件名并限制在原子分配的 Run 目录内。

### 3.9 Data Browser

- GRAPH-001：Browser SHALL 不与当前 Run DAT 自动绑定。
- GRAPH-002：拖入/打开的 DAT 更新时 SHALL 自动刷新。
- GRAPH-003：Y 选择 SHALL 支持一次多选确认。
- GRAPH-004：SHALL 支持多 Y Overlay 或多图共享 X。
- GRAPH-005：X/Y SHALL 可独立切换 Log。
- GRAPH-006：SHALL 支持框选放大、双击最近点详情。
- GRAPH-007：显示配置 SHALL 保存为 DAT 同目录同 stem `.plt`。

### 3.10 事件

- EVT-001：事件键 SHALL 为 Source+Code+Context。
- EVT-002：同一活动 Warning/Error SHALL 只弹一次并累加 Count。
- EVT-003：Resolve 后再次发生 SHALL 可重新弹窗。
- EVT-004：Warning SHALL 继续 SEQ。
- EVT-005：Error SHALL 使 Running/Paused SEQ Faulted。
- EVT-006：所有 Raised/Resolved SHALL 写 events.dat。

## 4. 非功能需求

- NFR-001：核心 SHALL 运行于 Python 3.11+。
- NFR-002：不得要求 C#；真实驱动可使用 Python 包装的厂商 SDK。
- NFR-003：GUI 线程不得执行阻塞仪表 I/O。
- NFR-004：默认仿真 SHALL 不控制真实仪器。
- NFR-005：逐行 Flush SHALL 默认启用。
- NFR-006：源码、示例、配置、扩展仓库模板和文档 SHALL 随发布包提供；活动扩展目录默认
  SHALL 为空。
- NFR-007：模块源码视为受信任；文档 SHALL 明确非安全沙箱。
- NFR-008：关键生命周期、解析、数据、事件和 UI SHALL 有自动测试。
- NFR-009：Windows EXE SHALL 写入与应用版本一致的文件/产品版本；外置可维护资源不得在 `_internal/` 重复打包。
- NFR-010：正式构建 SHALL 使用经验证的精确依赖锁定，不得在构建时隐式升级工具链。

## 5. 状态机

### 5.1 SEQ

```text
Idle/Stopped/Completed/Faulted
             │ Run
             ▼
          Running ↔ Paused
             │ Stop/Error
             ▼
          Stopping
             ├─ stopped
             └─ faulted
```

结束清理失败可把原 Completed/Stopped/Faulted 最终统一提升为 Faulted。

### 5.2 Module

```text
Disabled → Initializing → Enabled ↔ Measuring
    ▲                        │
    │        Disable         ▼
    └──────── Disabling ←────┘

任一运行阶段可进入 Faulted，但 Enabled 标志是否保留由操作决定：
- initialize 失败：Disabled
- end 失败：Enabled + Faulted
- Disable abort 失败：报告 Error，工作进程有界强制关闭，最终 Disabled；不声明仪表安全
```

## 6. 数据契约

Run 目录 SHALL 包含：

```text
sequence.seq
configuration.toml
module_settings/<id>.settings.toml
module_settings/<id>.status-at-start.json
experiment.dat
events.dat
```

具体列和事件格式见 [DAT_FORMAT.md](DAT_FORMAT.md)，模块 API 见 [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md)。

## 7. 安全约束

- SAF-001：真实温场上下限和最大速率必须由主配置提供。
- SAF-002：Stop/Error SHALL Hold Current，不自动归零或断电，也不保留旧 Target。
- SAF-003：模块启用不得自动 Apply 保存设置。
- SAF-004：模块关闭 SHALL 先尝试 abort；abort 失败仍 SHALL 保留 Error，并有界强制回收工作进程。
- SAF-005：设备/模块通信必须配置有限超时。
- SAF-006：禁止在模块/设备源码中提交秘密。
- SAF-007：接入真实硬件必须按测试计划分阶段完成。
- SAF-008：扩展安装 SHALL 完全离线、内容可审查、依赖可复现；信任不得仅绑定路径或名称。
- SAF-009：软件进程终止不得被解释为仪表已进入安全状态。

## 8. 验收基线

版本可发布必须同时满足：

1. 自动测试全部通过。
2. Source GUI offscreen smoke 通过且截图可读。
3. 从独立仓库模板手动复制的示例模块完成信任、Enable/Apply/Manual/Measure/End/Disable
   独立进程测试。
4. 一次 Measure 产生 R1–R4 四个顺序行和每行系统快照。
5. 无模块 Measure 产生 Warning + 一行系统状态并完成。
6. 旧 Measure 参数和 Initialize 被解析 Error 拒绝。
7. Windows 文件夹发布包构建成功，包含空 modules/device_plugins、两个仓库模板和 docs，
   且 `_internal/` 无资源副本。
8. 发布 EXE GUI smoke 与 headless demo 通过。
9. EXE 文件版本和产品版本与应用版本一致。
10. Git 作者唯一为 `yang-fu-physics <yfu.physics@gmail.com>`。
