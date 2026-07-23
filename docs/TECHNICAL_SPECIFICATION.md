# OpenLab Control 0.10.2 技术规格

状态：Implemented Baseline
日期：2026-07-23
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
- 仿真设备与 `simulated_transport` 示例模块；
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
- SEQ-011：Stop/Error 后温度与磁场 SHALL 按配置保持当前或目标；默认 Hold Current。

### 3.3 设备控制

- DEV-001：Device kind SHALL 仅为 temperature、field、monitor。
- DEV-002：每个设备的 Poll/Set/Hold SHALL 由同一异步锁串行化。
- DEV-003：不同设备 MAY 并发轮询。
- DEV-004：Target 与 Rate SHALL 同时在 UI 和运行时由同一配置限制。
- DEV-005：中央 SHALL 使用偏差、窗口斜率、Dwell、Timeout 判稳。
- DEV-006：模块 SHALL 只能获得设备只读快照，不得获得控制引用。

### 3.4 Measurement Module 发现与依赖

- MOD-001：模块根目录 SHALL 可配置，默认 `modules/`。
- MOD-002：启动和合法 Refresh SHALL 扫描一级子目录 `module.toml`。
- MOD-003：每次应用启动所有模块 SHALL 为 Disabled。
- MOD-004：Manager SHALL 只显示 Enabled、Name、Version 三列。
- MOD-005：Refresh SHALL 仅在 SEQ Idle 且所有模块 Disabled 时允许。
- MOD-006：清单 SHALL 验证唯一 ID、API、入口、backend type、固定列和依赖。
- MOD-007：缺失依赖或冲突依赖 SHALL 禁止 Enable。
- MOD-008：依赖 SHALL 默认共享同一 `module_runtime/site-packages`，不得自动创建逐模块环境。
- MOD-009：Install Dependencies SHALL 显式触发；Enable 不得自动安装。
- MOD-010：修改共享依赖前 SHALL 要求全部模块 Disabled。
- MOD-010：离线 wheels SHALL 优先；在线 pip SHALL 再次取得用户确认。

### 3.5 模块进程和界面

- PROC-001：每个 Enabled 模块 backend SHALL 在独立 spawn 工作进程运行。
- PROC-002：frontend SHALL 在 GUI 进程/线程运行。
- PROC-003：frontend SHALL 不得直接执行 VISA/Serial/SDK I/O。
- PROC-004：同一模块 IPC 操作 SHALL 串行。
- PROC-005：真实驱动 SHALL 自行配置有限通信超时；框架不添加统一生命周期超时。
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
- LIFE-010：Disable abort 失败 SHALL 保持 Enabled/可见并报告 Error。

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
- DATA-004：未声明列/不支持值类型 SHALL Error。
- DATA-005：模块 SHALL 自行声明业务 Status/Warning 列；框架不加通用列。
- DATA-006：Warning/Error 时可用温场/Monitor 数据 SHALL 保留。

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
- NFR-006：源码、示例、配置、模块和文档 SHALL 随发布包提供。
- NFR-007：模块源码视为受信任；文档 SHALL 明确非安全沙箱。
- NFR-008：关键生命周期、解析、数据、事件和 UI SHALL 有自动测试。

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
- end/abort 失败：Enabled + Faulted
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
- SAF-002：Stop/Error 默认 Hold Current，不自动归零或断电。
- SAF-003：模块启用不得自动 Apply 保存设置。
- SAF-004：模块关闭不得绕过 abort 成功状态。
- SAF-005：设备/模块通信必须配置有限超时。
- SAF-006：禁止在模块/设备源码中提交秘密。
- SAF-007：接入真实硬件必须按测试计划分阶段完成。

## 8. 验收基线

版本可发布必须同时满足：

1. 自动测试全部通过。
2. Source GUI offscreen smoke 通过且截图可读。
3. 示例模块完成 Enable/Apply/Manual/Measure/End/Disable 独立进程测试。
4. 一次 Measure 产生 R1–R4 四个顺序行和每行系统快照。
5. 无模块 Measure 产生 Warning + 一行系统状态并完成。
6. 旧 Measure 参数和 Initialize 被解析 Error 拒绝。
7. Windows 文件夹发布包构建成功，包含 modules/templates/docs。
8. 发布 EXE GUI smoke 与 headless demo 通过。
9. Git 作者唯一为 `yang-fu-physics <yfu.physics@gmail.com>`。
