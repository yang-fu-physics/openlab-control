# 测试与真实设备上线清单

## 自动测试

运行：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试文件与重点：

| 文件 | 重点 |
|---|---|
| `test_sequence_parser.py` | 单行语法、任意嵌套、List、T/F、旧命令与越界/非有限参数拒绝 |
| `test_sequence_editor.py` | 多行右键/键盘、父子去重、运行锁定 |
| `test_engine.py` | 嵌套 Scan、Pause 时钟、Hold、控制等待上限、运行时参数防绕过与展开进度 |
| `test_measurement_modules.py` | 清单 mode、逻辑槽位并集、每通道合并行、Settings、信任、IPC/退出超时、完整生命周期 |
| `test_extension_dependencies.py` | 框架共享版本划分、额外依赖精确哈希 lock、离线安装和 runtime 篡改 |
| `test_device_plugin_manifest.py` | 外部设备清单、core/API、内容指纹和首次信任 |
| `test_device_worker.py` | 每设备独立进程、阻塞强杀、依赖只在子进程可见 |
| `test_device_recovery.py` | 读链路重连、恢复状态核对、写超时不重放、最终故障 |
| `test_datafile.py` | 动态列、同槽位多模块合并、追加 Schema、路径限制、原子运行目录与快照 |
| `test_devices_and_units.py` | 控制/Monitor 插件、竞态、限制、非有限值、设备超时隔离、Hold 与 Oe/T |
| `test_events_and_stability.py` | 活动事件去重、重复 Error 仍中止、数值判稳与超时 |
| `test_main_window.py` | MDI、SEQ 重开、长路径、配置限制 |
| `test_data_browser.py` | 多 Y、Overlay/Stacked、Log、刷新、最近点 |
| `test_plot_format.py` | `.plt` 保存、恢复和回退 |
| `test_status_tile.py` | Monitor 只读、格式精度、参数弹窗 |
| `test_ui_scaling.py` | 1080p/2K/4K 自动和手动缩放 |
| `test_release_contract.py` | 版本同步、依赖锁定、源码入口与发布资源布局 |
| `test_repository_templates.py` | 核心扩展目录默认空、两个独立仓库模板与清单有效性 |

任何发布版本必须 100% 通过。失败不得通过删除测试或扩大安全容差掩盖。

## 源码冒烟

### GUI

```text
.venv\Scripts\python.exe run.py --gui-smoke --screenshot source-gui-smoke.png
```

验收：退出码 0；截图非空；主窗口只有温度、磁场和 Monitor 状态块；工具栏有 Modules；SEQ 与右栏可读。

### 无界面

```text
.venv\Scripts\python.exe run.py --headless-demo --sequence examples\module_measurement.seq --timeout 30
```

由于每次启动模块默认 Disabled，预期 Measure 报一个 `NO_ENABLED_MODULES` Warning、写系统状态行并 Completed。

再验证独立模块进程：

```text
.venv\Scripts\python.exe run.py --headless-demo --enable-module simulated_transport --sequence examples\module_measurement.seq --timeout 30
```

预期 Completed；示例模块声明四个 `aligned_slots`，3 次 Measure 各按 R1–R4 写四个
逻辑通道行，共 12 行模块数据。`--enable-module` 只用于无界面验收，不改变 GUI 每次
启动全部 Disabled 的规则。
执行前必须从仓库模板复制模块，并通过 GUI 预先信任完全相同的内容指纹；无界面模式不得
自动信任。只有声明框架未提供额外依赖的模块才必须先完成离线 runtime 准备。

### 模块视觉预览

```text
.venv\Scripts\python.exe tools\capture_module_preview.py
```

验收：生成 Manager、Settings、Status 三张预览；Manager 只有三列；模块默认页是 Settings；Apply 按钮只在 Settings 预览中存在。

## SEQ 验收

1. 新建 SEQ，逐一双击右栏命令并插入。
2. 双击已有命令，确认参数回填。
3. 嵌套 Temperature → Field → Time → Measure，保存、重开，层级不变。
4. Linear/List 切换正确；List 保留重复和回扫。
5. 输入越界 List，确认在移动前拒绝。
6. 多选父 Scan 和子行，Delete/Copy 只处理最外层。
7. Disable Scan 后子 Measure 不执行；Enable 后恢复。
8. 手工写 `Measure devices=...`，Run 被 Validation Error 阻止。
9. 手工写 Initialize，Run 被 Validation Error 阻止。
10. Running 时编辑/模块变更锁定，Copy 可用。

## Modules Manager 验收

1. 重新启动，确认所有模块 Disabled。
2. 从独立仓库模板复制示例模块，首次 Enable 核对并确认内容指纹；初始化期间行不可操作，
   成功后才勾选。
3. 双击 Enabled 行，窗口置前。
4. 尝试关闭/Alt+F4，窗口仍存在。
5. 最小化主窗口，模块窗口一起最小化；恢复后恢复。
6. Settings 页显示 Apply Settings；切换到 Status 后按钮完全隐藏。
7. 将模块窗口缩到内容安全边界，确认不能继续缩小且内容无裁切。
8. Disable 成功，窗口隐藏。
9. SEQ 运行中 Enable/Disable/Refresh/Install 均不可用。
10. 所有模块 Disabled 时 Refresh 生效。
11. 只声明 PyVISA 等框架共享依赖时，Install Dependencies 完全隐藏且可直接 Enable。
12. 存在额外依赖时才显示 Install Dependencies；目标模块 Enabled 时安装被阻止，其他
    隔离模块 Enabled 不影响目标。
13. 制造共享依赖范围不兼容或其他无效 manifest，程序仍启动，该模块禁止 Enable 并
    显示原因。
14. 修改已信任源码，确认旧信任失效且 Enable 前再次提示。
15. 删除 manifest 的 `measurement_mode`，确认显示 Warning、仍允许 Enable，且按
    `once_per_slot` 执行；正式示例清单必须显式声明。

## Settings/Status 验收

1. 修改 Settings 后关闭应用，确认 `module_data/<id>/settings.toml` 保存。
2. 重启 Enable，值自动载入，但 Status 显示未 Apply。
3. Apply 取消时不发送；确认时发送并更新状态。
4. 未 Apply 修改后 Run，逐一验证三个选项。
5. Run Without Applying 后检查运行快照：settings 是界面值，status JSON 是实际值。
6. Running 时 Settings 灰化，Status 可继续显示。
7. 手动 Measure Now 只更新 Status/Run Log，不增加 experiment.dat 行。
8. Enabled 模块修改 Settings 后保存 SEQ，确认生成同名 `.modules.toml` 并记录当前值。
9. 重新启动、保持全部模块 Disabled 后 Load SEQ，确认设置已导入但没有初始化 worker。
10. 模块已 Enabled 时 Load 另一 SEQ，确认 Settings 页切换并标记未 Apply，后台没有
    `apply_settings` 调用。
11. Load 无伴随文件的旧 SEQ，确认继续读取模块自己的持久设置。
12. Load 运行目录 `sequence.seq`，确认导入 `module_settings/*.settings.toml`。
13. 制造损坏、超大、错误格式版本、非法模块 ID 和模块版本变化，确认 Warning/fail
    closed，SEQ 文本仍能打开且仪表不收到命令。
14. Call Sequence 指向带伴随文件的子 SEQ，确认运行中不会隐式切换模块设置。

## 生命周期故障注入

为测试模块分别让以下函数抛异常：

| 阶段 | 预期 |
|---|---|
| initialize | Disabled、Error、无窗口或窗口不显示、工作进程退出 |
| apply_settings | 保持 Enabled、未标 Applied、Error |
| begin_sequence | Run Faulted、调用 end_sequence(error)、不 abort |
| measure Warning | 继续、有效行保留、一次活动弹窗 |
| measure Error | Run Faulted、其他已到达行保留、end(error)、不 abort |
| end_sequence(completed) | 原完成改为 Faulted、模块 Enabled、Status 可见、不 abort |
| abort on Disable | Error、工作进程有界强制关闭、最终 Disabled；提示不代表仪表安全 |

记录每个阶段的调用顺序，确保不会把 Stop/Error 误当 Disable。

## 逻辑槽位、并行与数据验收

1. Enable 两个 `aligned_slots` 模块：A 使用 `[1,3,4]`，B 使用 `[1,2,4]`；再 Enable
   一个 `once_per_slot` 模块。
2. 一条 Measure 产生四行；A/B 在同槽位并行，`once_per_slot` 每行调用一次。
3. 第 2 行 A 为空、第 3 行 B 为空，其他参与模块值出现在同一通道行。
4. 中央完成四个槽位后才执行下一条 Remark/Set。
5. 每个逻辑通道行只有一份核心温度/场/Monitor 快照且采样时间合理。
6. 每个模块列有 ID 前缀，无碰撞；各模块 rawdata 仍写独立 sidecar。
7. 单次调用无行、两个 emit 或 emit 后 return，确认 Error/Faulted。
8. 发未声明列，确认 Error/Faulted。
9. 发复杂对象值，确认类型 Error。
10. Stop 发生在当前槽位时不写半成品行，前序完整槽位保留。
11. 无模块 Measure 写恰好一行系统状态并继续。

## Warning/Error 去重验收

1. 同一 module/code/context 连续 10 次 Warning。
2. 只出现一个弹窗；events.dat Raised 一次，Count 在活动对象累加。
3. Resolve 后再次触发，允许新弹窗。
4. context 改为另一通道，允许独立弹窗。
5. Error 在 Idle 只报警；在 Running/Paused 中止 SEQ。
6. Error 后确认模块只 end(error)，未 abort。

## Data Browser 验收

1. 打开非当前 Run 的任意 DAT。
2. 外部追加行，图自动刷新且保持 `.plt` 格式。
3. 一次勾选多个 Y 后对话框才关闭。
4. Overlay 与 Stacked 共享 X 正确。
5. X/Y Log 独立；非正点不绘制且不崩溃。
6. 框选放大、Reset Zoom。
7. 双击数据点显示完整源行。
8. 关闭重开，`.plt` 恢复；DAT 列变化时安全回退。
9. 打开带 `FILEOPENTIME` 的 Quantum Design DAT，确认 `Time Stamp (sec)` 显示
   实际仪表日期时间，点详情仍保留原始秒值。
10. 分别打开带 `TIMESTAMP_EPOCH=labview_1904` 和 `unix` 的 OpenLab DAT，确认
    `Started` 校准结果正确；无可靠元数据且数值不在保守范围内时不转换。
11. 检查正负温度、磁场和小数范围，线性刻度步长只采用 `1/2/5 × 10ⁿ`，不出现
    任意等分小数。
12. 把时间轴缩放到亚秒、跨午夜、跨日期范围，确认标签不重复且日期上下文明确。

## 设备状态日志与 Live Trend 验收

1. 启动包含温度、磁场和 Monitor 的仿真 Run，确认自动目录出现
   `device_status.dat`。
2. 检查初始行以及后续 Current、Target、Rate、Activity、Stability、Connection、
   Connected、ReadingAge、Message。
3. 把状态周期设为 1 秒、轮询设为 0.2 秒，确认约每秒一行且没有额外设备查询。
4. 分别制造 moving、stable、stale、reconnecting 和 faulted，确认状态文字及消息落盘。
5. 选择 external 实验 DAT，确认状态文件仍留在运行目录。
6. 尝试把实验 DAT 指向 events、device status、SEQ、配置或模块设置快照，确认拒绝且
   原文件不变。
7. Live Trend 隐藏时持续接收有界历史但不重绘；打开后最多每 250 ms 一次重绘。
8. 6 条曲线各 900 点连续更新，确认 GUI 可操作、设备 poll 不超时、状态日志周期不变。

## Windows 发布包验证

```text
build.bat
```

检查：

- `dist/OpenLabControl/OpenLabControl.exe`；
- `configs/`、`examples/`、`docs/`、空 `modules/`、空 `device_plugins/` 和
  `plugin_templates/` 中两个仓库模板；
- 可写 `runs/`、`module_data/`、`plugin_runtime/`、`plugin_state/`、`wheels/`；
- `_internal/` 不重复包含上述外置资源；
- EXE 文件版本和产品版本与应用版本一致；
- `_internal` 包含核心统一的 PyVISA 1.16.2，复制 372A 后无需
  `plugin_runtime`/Install Dependencies 即可完成 Enable 和资源发现；
- 记录 Authenticode 签名状态；未签名包必须在发布说明中明确标注；
- EXE GUI smoke；
- EXE headless demo（默认无模块，以及手动复制并预置信任 `simulated_transport` 两种）；
- 在没有开发仓库/PYTHONPATH 的干净目录中，核心默认发现 0 个模块；复制模板后发现
  `simulated_transport`。

## 真实设备上线前

### 文档与接线

- [ ] 型号、序列号、固件、接口、地址记录。
- [ ] 线缆、接地、屏蔽、急停、互锁和最大允许输出记录。
- [ ] 厂商手册中的通信/状态/错误码映射完成。
- [ ] 每个通信操作有限超时。
- [ ] 人工恢复/断电流程可在 UI 不工作时执行。

### Device Plugin

- [ ] 审查并手动复制目标 Device Plugin，首次内容指纹和框架共享依赖范围验证通过；
  如有额外依赖，再验证离线 runtime；核心自带示例不得直接当作真实仪表驱动。
- [ ] 每个温度/磁场 kind 只有一个 primary；所有 secondary 默认只读。
- [ ] 只读 connect/poll 连续运行至少 1 小时。
- [ ] connect 核对型号/固件且不改变仪表输出或自动 Apply 面板设置。
- [ ] 温场单位、符号和速率换算与独立仪表核对。
- [ ] 最小风险 Target；Stable 判定与人工判断对比。
- [ ] 主配置、手动弹窗、手写 SEQ、运行时和插件侧上下限均无法绕过。
- [ ] Stop/Error Hold 使用新鲜读回，不能使用缓存或默认零。
- [ ] 拔线后进入 Reconnecting，恢复期间 SEQ 计时冻结，60 秒上限后 Fault。
- [ ] 恢复后读取并核对实际 target/rate，不会重复下发危险目标。
- [ ] 模拟写超时，确认没有自动重放。
- [ ] 强制终止设备进程后其他设备仍响应，退出后无残留句柄/子进程。

### Measurement Module

- [ ] Enable 只初始化，不改变源输出/范围。
- [ ] 首次信任绑定准确内容；改一字节后旧信任失效。
- [ ] 离线 lock/wheel 哈希和 runtime 内容篡改都会阻止 Enable。
- [ ] Settings 与实际 Status 逐项核对。
- [ ] Apply 顺序、范围、互锁经过低风险测试。
- [ ] begin/measure/end/abort 每条命令有仪表侧证据。
- [ ] completed/stopped/error 后输出状态符合设计。
- [ ] Disable abort 真实关闭/退出所需输出状态。
- [ ] R1–R4 数据、单位、极性、时间戳与独立读数一致。
- [ ] 超量程为 Warning；硬件报警/互锁/关键温度为 Error。
- [ ] 多模块不会争用同一物理接口/仪表。

### 长时与恢复

- [ ] 典型完整 SEQ 小范围运行成功。
- [ ] 8–24 小时长时运行无句柄/内存/文件增长异常。
- [ ] 拔线、仪表关机、网络中断、磁盘不可写、应用关闭均演练。
- [ ] 运行目录能用 SEQ+配置+Settings+Status+events 完整复盘。

未完成以上真实硬件清单前，不得把仿真通过等同于设备安全认证。
