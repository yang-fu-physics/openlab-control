# OpenLab Control 0.10.1 验证报告

- 验证日期：2026-07-23
- 验证平台：Windows 11 x64（build 26200）
- 源码运行时：Python 3.13.2、PySide6 6.11.1
- 打包工具：PyInstaller 6.21.0（onedir）
- 自动测试：71 项通过，0 项失败

## 结论

0.10.1 的仿真框架和 Windows 发布包达到本阶段交付条件。测量方案已与温度、磁场和只读 Monitor 设备体系分离；模块可从配置目录发现，在独立进程内运行仪表后端，并由主界面提供自定义 Settings/Status 窗口。无参数 SEQ `Measure` 可并行等待所有 Enabled 模块，并把模块流式多行结果与中央温度、磁场、Monitor 快照统一写入 DAT。

默认配置和示例模块均为仿真。本报告不代表任何真实温控仪、磁体电源、Keithley、Lakeshore 372 或其他硬件通过安全认证。

## 自动测试

运行命令：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：71 项通过，0 项失败。主要覆盖：

- 模块清单发现、API/Schema 校验、共享依赖范围冲突和设置往返。
- initialize、apply_settings、begin_sequence、measure、end_sequence、abort 完整生命周期。
- 多个模块同时开始测量、单模块多行流式结果、中央等待全部完成。
- 无 Enabled 模块时 Warning 去重、系统状态行和继续执行。
- measure/end/abort 失败语义；Error 调用 end(error) 而不自动 abort。
- 模块窗口 Settings/Status、Settings 专属 Apply、内容安全最小尺寸、未应用编辑检测和禁止用户关闭。
- 运行目录的 SEQ、主配置、desired settings、实际 Status、实验 DAT 和事件 DAT 快照。
- 旧 Initialize 与带参数 Measure 的解析 Error 及 GUI Run 阻止。
- 温度/磁场扫描、Temperature List、任意嵌套、Hold、Warning/Error 和数值判稳。
- SEQ 多行选择及 Disable/Enable/Delete/Copy/Paste 的鼠标和键盘操作。
- 温度三位、Oe 两位、配置限制、手动控制、只读 `2nd Stage` Monitor。
- Data Browser 多 Y、Overlay/Stacked、共享 X、X/Y Log、自动刷新、点详情和 `.plt` 恢复。
- 长路径侧栏、关闭后重建 SEQ 子窗口和 1080p/2K/4K 界面缩放。

`compileall` 和 `git diff --check` 同时通过。

## 源码端到端验证

### GUI

源码以离屏 GUI 模式启动、轮询三个设备状态块、渲染完整主窗口并正常关闭，退出码 0。人工检查截图确认：

- 底部只有 Temperature、Magnetic Field、`2nd Stage`，没有旧 Transport 状态块；
- 工具栏和菜单均有 Modules；
- SEQ 编辑器、右侧命令栏、英文主界面和精度显示正常；
- Modules Manager 只有 `Enabled / Name / Version` 三列；
- 示例模块窗口默认显示 Settings；Apply Settings 只在该页出现，Status 页不显示；窗口到内容安全边界后不能继续缩小。

### 无模块运行

运行 `examples/module_measurement.seq`，保持所有模块 Disabled：

- 退出码 0，最终状态 Completed；
- 首次 Measure 产生一个 `NO_ENABLED_MODULES` Warning；
- 后续相同活动 Warning 不重复弹出/记录 Raised；
- 3 次 Measure 各写一行温度、磁场和 Monitor 系统状态。

### 启用示例模块

无界面验收使用 `--enable-module simulated_transport` 显式启用模块：

- 退出码 0，最终状态 Completed；
- 模块后端在独立 spawn 进程中完成 initialize、begin、3 次 measure 和 end(completed)；
- 每次 Measure 依次流式产生 R1、R2、R3、R4，共 12 行；
- 每行到达时重新采集中央温度、磁场和 `2nd Stage`，没有把四个通道伪装成同一采样时刻；
- DAT Schema 固定为 14 列，模块列自动使用 `simulated_transport.` 前缀；
- 运行目录含 configuration、sequence、module settings、status-at-start、experiment.dat 和 events.dat。

## Windows 发布包验证

重新构建 `dist/OpenLabControl`，并确认发布目录包含：

- `OpenLabControl.exe`；
- `configs/`、`examples/`、`docs/`、`modules/`、`plugin_templates/`；
- 可维护的 `runs/`、`module_data/`、`module_runtime/site-packages/` 和 `wheels/`。

对打包 EXE 独立执行三项验证：

| 场景 | 结果 |
|---|---|
| 离屏 GUI 启动、截图、关闭 | 退出码 0 |
| 所有模块 Disabled 的 headless demo | 退出码 0，Completed |
| 启用 `simulated_transport` 的 headless demo | 退出码 0，Completed |

打包后的模块验证得到 12 行数据、14 列固定 Schema；`BYAPP` 版本为 `0.10.1`。这同时验证了 EXE 边界后的模块源码发现、独立工作进程、IPC、流式行写入和生命周期收尾。

## 界面验收图

- 主窗口：`docs/main-window-preview.png`
- 模块管理器：`docs/module-manager-preview.png`
- 模块窗口：`docs/module-window-preview.png`
- 模块 Status 页：`docs/module-status-preview.png`
- SEQ 多行菜单：`docs/sequence-context-menu-preview.png`
- Data Browser：`docs/data-browser-preview.png`

## 尚未验证

- 任何真实 GPIB、VISA、串口、以太网或厂商 SDK 通信。
- 实际磁体、低温系统、测量源表的硬件联锁和量程保护。
- 断电、线缆脱落、驱动崩溃、网络中断和磁盘写满等物理故障。
- 数小时至数天的真实设备长时运行。

接入真实设备前，必须按 `docs/TEST_PLAN.md` 完成 Device Plugin 与 Measurement Module 两部分上线清单，并为每个通信操作设置驱动级超时。
