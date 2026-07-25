# OpenLab Control 0.10.3 验证报告

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64（build 26200）
- 源码运行时：Python 3.13.2、PySide6 6.11.1
- 打包工具：PyInstaller 6.21.0（onedir）
- 自动测试：110 项通过，0 项失败
- Windows 包：290 个文件，138825659 字节（约 132.39 MiB）
- EXE 文件/产品版本：0.10.3 / 0.10.3
- Authenticode：NotSigned

## 结论

0.10.3 的自动测试、源码端到端流程和 Windows 干净目录发布包均通过本次仿真验证，可以发布为未签名修复版本。源码版与打包版在 GUI 启动、默认模块 Disabled、无模块 Measure、启用独立模块、多行 DAT 和进程退出方面结果一致。

本结论仅适用于当前仿真配置。它不代表任何真实温控仪、磁体电源、Keithley、Lakeshore 372 或其他硬件通过安全认证。真实仪表仍须完成驱动级超时、硬件联锁、最小风险目标、断线、断电和 Hold 实测后才可接入。

## 自动测试

运行：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

完整测试在发布前独立执行一次，并由 `build.bat` 再执行一次；两次均为 110 项通过、0 项失败。独立测试阶段将 `DeprecationWarning` 视为错误。主要覆盖：

- Error/Warning 活动去重、重复 Error 的致命传播、恢复状态和判稳。
- Wait/Scan Time 的 Pause 时钟冻结、Stop/Error、强制 Hold Current 和 Hold 失败状态。
- 手写与运行时 SEQ 参数边界、非有限值、任意嵌套、Call Sequence 展开进度和设备 ID 保持。
- 设备 Poll/Set/Hold 竞态、操作超时、超时后隔离、无新读数的控制等待上限和异常恢复。
- 模块 initialize/apply/begin/measure/end/abort 生命周期、独立进程 IPC 超时、强制退出、并行测量和流式多行结果。
- DAT `open/create/open|create`、追加 Schema、动态列、多行结果、路径限制、原子运行目录、事件与设置/状态快照。
- Data Browser 任意 DAT、自动刷新、多 Y、Overlay/Stacked、共享 X、Log、点详情和 `.plt` 恢复。
- 1080p/2K/4K 缩放、浮动窗口约束、关闭后重建、模块窗口生命周期和临时对话框销毁。
- 版本同步、精确依赖锁、源码入口与 Windows 外置资源布局。

附加检查：

- `compileall`：通过。
- `pip check`：`No broken requirements found`。
- `git diff --check`：通过。
- PyInstaller 警告文件中的未解析条目未在三项实际执行路径造成加载失败；干净包 GUI、模块工作进程和 headless 验收均正常。

## 源码端到端验证

### GUI

源码以 `--gui-smoke` 离屏启动，轮询三个设备状态块、截取 1480 × 900 主窗口并正常关闭，退出码 0。人工检查确认：

- Temperature、Magnetic Field 与只读 `2nd Stage` 均显示正常；
- Modules 菜单、SEQ 编辑器、命令栏、按钮状态与英文界面无裁切；
- 默认显示 `0 of 1 measurement modules enabled`，符合每次启动全部 Disabled 的设计；
- 自动化 GUI 测试另行覆盖 4K 缩放、浮动窗口、SEQ 重建、Settings/Status 和窗口销毁。

### 无模块运行

运行 `examples/module_measurement.seq`，保持模块 Disabled：

- 退出码 0，最终状态 Completed；
- `NO_ENABLED_MODULES` Warning 只 Raised 一次，后续 Measure 继续；
- DAT 为 3 行、8 列，`BYAPP` 为 `OpenLab Control,0.10.3`。

### 启用示例模块

使用 `--enable-module simulated_transport`：

- 退出码 0，最终状态 Completed；
- 后端在独立 spawn 进程完成 initialize、begin、3 次 measure 和 end(completed)；
- 3 次 Measure 各流式返回 R1–R4，共 12 行、14 列；
- 模块列带 `simulated_transport.` 前缀；
- 运行目录包含 DAT、事件、SEQ、配置以及模块 settings/status 快照。

## Windows 发布包验证

最终构建位于 `dist/OpenLabControl`。确认存在：

- `OpenLabControl.exe`；
- `configs/`、`examples/`、`docs/`、`modules/`、`plugin_templates/`；
- 可维护的 `runs/`、`module_data/`、`module_runtime/site-packages/` 和 `wheels/`；
- 上述资源均未在 `_internal/` 重复。

将发布目录复制到不含源码和开发 `PYTHONPATH` 的干净目录后执行：

| 场景 | 结果 |
|---|---|
| 离屏 GUI 启动、截图、关闭 | 退出码 0，截图与源码版一致 |
| 所有模块 Disabled 的 headless demo | 退出码 0，Completed，3 行/8 列 |
| 启用 `simulated_transport` 的 headless demo | 退出码 0，Completed，12 行/14 列 |

两个打包 DAT 的 `BYAPP` 均为 0.10.3；测试结束后没有残留 `OpenLabControl` 进程。EXE 的 SHA-256 在最终压缩发布包生成时另行记录。

## 签名与发布边界

本次 EXE 未配置代码签名证书，Authenticode 状态为 `NotSigned`。这不会改变上述运行结果，但 Windows SmartScreen 或组织策略可能提示未知发布者。发布页面和校验文件必须明确这一点，不得描述为已签名。

## 尚未验证

- 任何真实 GPIB、VISA、串口、以太网或厂商 SDK 通信。
- 实际磁体、低温系统、测量源表的硬件联锁和量程保护。
- 底层驱动调用在框架超时后仍不可取消时的进程级隔离。
- 断电、线缆脱落、驱动崩溃、网络中断、磁盘写满和数小时至数天长时运行。

接入真实设备前，必须逐项完成 `docs/TEST_PLAN.md` 的真实设备上线清单，并为每个通信操作设置比框架上限更短的驱动级超时。
