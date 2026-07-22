# 测试与真实设备上线清单

## 自动测试

运行：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试文件与重点：

| 文件 | 重点 |
|---|---|
| `test_sequence_parser.py` | 单行语法、任意嵌套、List、T/F、旧 Initialize/Measure 参数拒绝 |
| `test_sequence_editor.py` | 多行右键/键盘、父子去重、运行锁定 |
| `test_engine.py` | Scan、Hold、Warning/Error、模块进程 Measure、运行目录 |
| `test_measurement_modules.py` | 清单、依赖冲突、Settings、完整生命周期、无模块、不可关闭窗口 |
| `test_datafile.py` | 模块前缀列、Monitor、Settings/Status 快照、事件 Resolve |
| `test_devices_and_units.py` | 控制/Monitor 插件、锁、限制与 Oe/T |
| `test_events_and_stability.py` | 活动事件去重、数值判稳与超时 |
| `test_main_window.py` | MDI、SEQ 重开、长路径、配置限制 |
| `test_data_browser.py` | 多 Y、Overlay/Stacked、Log、刷新、最近点 |
| `test_plot_format.py` | `.plt` 保存、恢复和回退 |
| `test_status_tile.py` | Monitor 只读、格式精度、参数弹窗 |
| `test_ui_scaling.py` | 1080p/2K/4K 自动和手动缩放 |

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

预期 Completed；3 次 Measure 各流式写入 R1–R4，共 12 行模块数据。`--enable-module` 只用于无界面验收，不改变 GUI 每次启动全部 Disabled 的规则。

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
2. Enable 示例模块；初始化期间行不可操作，成功后才勾选。
3. 双击 Enabled 行，窗口置前。
4. 尝试关闭/Alt+F4，窗口仍存在。
5. 最小化主窗口，模块窗口一起最小化；恢复后恢复。
6. Settings 页显示 Apply Settings；切换到 Status 后按钮完全隐藏。
7. 将模块窗口缩到内容安全边界，确认不能继续缩小且内容无裁切。
8. Disable 成功，窗口隐藏。
9. SEQ 运行中 Enable/Disable/Refresh/Install 均不可用。
10. 所有模块 Disabled 时 Refresh 生效。
11. 任一模块 Enabled 时 Install Dependencies 被阻止。
12. 制造无效 manifest，程序仍启动，该模块禁止 Enable并显示原因。

## Settings/Status 验收

1. 修改 Settings 后关闭应用，确认 `module_data/<id>/settings.toml` 保存。
2. 重启 Enable，值自动载入，但 Status 显示未 Apply。
3. Apply 取消时不发送；确认时发送并更新状态。
4. 未 Apply 修改后 Run，逐一验证三个选项。
5. Run Without Applying 后检查运行快照：settings 是界面值，status JSON 是实际值。
6. Running 时 Settings 灰化，Status 可继续显示。
7. 手动 Measure Now 只更新 Status/Run Log，不增加 experiment.dat 行。

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
| abort on Disable | 仍 Enabled、窗口打开/Faulted、Error |

记录每个阶段的调用顺序，确保不会把 Stop/Error 误当 Disable。

## 并行与流式数据验收

1. Enable 两个仿真模块，让延时不同。
2. 一条 Measure 同时启动两者。
3. 各模块内部行顺序保持。
4. 模块间行按实际到达顺序混排。
5. 中央等待两者完成后才执行下一条 Remark/Set。
6. 每行温度/场/Monitor 可不同且采样时间合理。
7. 每个模块列有 ID 前缀，无碰撞。
8. 发未声明列，确认 Error/Faulted。
9. 发复杂对象值，确认类型 Error。
10. 无模块 Measure 写恰好一行系统状态并继续。

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

## Windows 发布包验证

```text
build.bat
```

检查：

- `dist/OpenLabControl/OpenLabControl.exe`；
- `configs/`、`examples/`、`docs/`、`modules/`、`plugin_templates/`；
- 可写 `runs/`、`module_data/`、`module_runtime/site-packages/`、`wheels/`；
- EXE GUI smoke；
- EXE headless demo（无模块和 `--enable-module simulated_transport` 两种）；
- 在没有开发仓库/PYTHONPATH 的干净目录仍能发现 `simulated_transport`。

## 真实设备上线前

### 文档与接线

- [ ] 型号、序列号、固件、接口、地址记录。
- [ ] 线缆、接地、屏蔽、急停、互锁和最大允许输出记录。
- [ ] 厂商手册中的通信/状态/错误码映射完成。
- [ ] 每个通信操作有限超时。
- [ ] 人工恢复/断电流程可在 UI 不工作时执行。

### Device Plugin

- [ ] 只读 connect/poll 连续运行至少 1 小时。
- [ ] 温场单位、符号和速率换算与独立仪表核对。
- [ ] 最小风险 Target；Stable 判定与人工判断对比。
- [ ] Stop/Error Hold 行为实测。
- [ ] 断线/重连不会重复下发危险目标。

### Measurement Module

- [ ] Enable 只初始化，不改变源输出/范围。
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
