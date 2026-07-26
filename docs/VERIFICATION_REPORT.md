# OpenLab Control 0.11.0b1 验证报告

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64（build 26200）
- 源码运行时：Python 3.13.2、PySide6 6.11.1
- 打包工具：PyInstaller 6.21.0（onedir）
- 版本名称：v0.11.0 Beta 1 / PEP 440 `0.11.0b1`
- 真实仪表：未连接、未验证

## 结论

0.11.0b1 的自动测试、源码端到端流程和 Windows 干净目录发布包均通过仿真验证。它适合
作为未签名 Beta 发布，供继续开发 Device Plugin 和 Measurement Module，不应描述为
稳定版或真实仪表安全认证版本。

源码版与打包版在以下行为一致：

- 核心 `modules/` 默认空，启动显示 0 个模块且全部 Disabled；
- 无模块 Measure 只 Raised 一次 Warning，SEQ 继续并写系统状态；
- 从独立仓库模板手动复制并信任的 `simulated_transport` 在子进程运行；
- 三次 Measure 各返回 R1–R4，共 12 行、14 列；
- GUI 离屏启动、设备子进程连接和应用关闭；
- DAT `BYAPP` 为 `OpenLab Control,0.11.0b1`。

## 自动测试

最终独立命令：

```text
.venv\Scripts\python.exe -W error -m unittest discover -s tests -v
```

结果：144 项通过，0 失败，0 error，未出现 Warning。

正式 `build.bat` 在 PyInstaller 前再次运行相同的 144 项测试：144 项通过，0 失败。

覆盖重点：

- SEQ 任意嵌套、List、Call、Pause 时钟冻结、Stop/取消/Hold 和运行时参数防绕过；
- primary/secondary/monitor 角色、手动/SEQ 限制同源和 Run 新鲜读回预检；
- 每设备独立进程、阻塞强杀、读链路恢复、恢复状态核对、写超时不重放和退出清理；
- 外部 Device Plugin/Measurement Module 清单、API/core、内容指纹和首次信任；
- 精确哈希 lock、完全离线 wheel 安装、runtime 篡改和扩展间依赖版本隔离；
- 模块 initialize/apply/begin/measure/end/abort、并行测量、多行、JSON IPC 和总关闭期限；
- DAT 动态列、追加 Schema、路径限制、运行快照、事件计数与多行结果；
- Data Browser 任意 DAT、自动刷新、多 Y、Overlay/Stacked、Log、点详情和 `.plt`；
- 1080p/2K/4K 缩放、浮动窗口、窗口重建、信号销毁和长路径布局；
- 核心空扩展目录、两个扩展仓库模板、文档契约、版本和 Windows 发布资源。

附加检查：

- `compileall -q src tests tools`：通过；
- `pip check`：`No broken requirements found`；
- `git diff --check`：通过；
- PyInstaller 仅报告 Windows 上预期的 POSIX/条件模块缺失；实际打包入口均通过。

## 源码端到端

### 默认无模块

`examples/module_measurement.seq`：

- 退出码 0，最终 Completed；
- `NO_ENABLED_MODULES` 只提醒一次；
- DAT 3 行、8 列；
- `BYAPP,OpenLab Control,0.11.0b1`。

### 模板模块

在独立临时项目中从
`plugin_templates/measurement-modules-repository/modules/simulated_transport` 手动复制，
写入与内容指纹匹配的信任记录，再显式 `--enable-module`：

- 退出码 0，最终 Completed；
- backend 在独立 spawn 进程运行；
- DAT 12 行、14 列，列带 `simulated_transport.` 前缀；
- 设置和运行开始 Status 快照均写入运行目录。

### GUI

源码 `--gui-smoke` 退出码 0，生成 1480 × 900 截图。人工检查：

- Temperature、Magnetic Field 和只读 `2nd Stage` 显示正常；
- `0 of 0 measurement modules enabled`；
- SEQ、右侧命令、工具栏和底部状态块无裁切；
- 设备连接完成后正常关闭。

## Windows 包

首次最终候选构建：

- `dist/OpenLabControl/OpenLabControl.exe` 存在；
- 289 个文件，138,389,968 字节（131.98 MiB）；
- FileVersion / ProductVersion：`0.11.0b1`；
- Authenticode：`NotSigned`；
- 外置 `configs/docs/examples/modules/device_plugins/plugin_templates/module_data/
  plugin_runtime/plugin_state/runs/wheels` 未在 `_internal` 重复。

把整个目录复制到不含源码和开发 `PYTHONPATH` 的干净位置后：

| 场景 | 结果 |
|---|---|
| 默认无模块 headless | 退出码 0，Completed |
| 离屏 GUI 截图与关闭 | 退出码 0，视觉结果与源码版一致 |
| 手动复制并预置信任模板模块 headless | 退出码 0，Completed，12 行/14 列 |
| 退出后进程检查 | 无残留 `OpenLabControl.exe` |

最终 ZIP 与 EXE SHA-256 写入发布包旁的 `SHA256SUMS.txt`，避免报告内容本身改变构建哈希。

## 未验证与发布边界

- 任何真实 GPIB、VISA、串口、以太网或厂商 SDK 通信；
- 真实温控仪、磁体电源、源表、表桥或切换器的量程、互锁和安全状态；
- 实际仪表在软件 worker 被 terminate/kill 后的物理输出；
- 驱动/固件差异、断电、磁盘写满和 8–24 小时真实硬件长时运行；
- Authenticode 签名与 SmartScreen 信任。

接入真实设备前，必须逐项完成 [TEST_PLAN.md](TEST_PLAN.md) 的真实设备上线清单。软件
进程隔离、超时和 Hold 尝试不能替代硬件限值、互锁、急停和人工恢复流程。
