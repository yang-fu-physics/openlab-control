# OpenLab Control 0.11.0b2 验证报告（历史归档）

> 归档说明：下文保留 Beta 2 发布前的验证记录与候选措辞。该标签后来已作为历史
> GitHub Release 发布，但不作为当前 Latest；当前稳定版本的结论以
> [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) 为准。

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64（build 26200）
- 源码运行时：Python 3.13.2、PySide6 6.11.1
- 打包工具：PyInstaller 6.21.0（onedir）
- 版本名称：v0.11.0 Beta 2 / PEP 440 `0.11.0b2`
- 发布状态：本地候选；尚未推送、打标签或发布
- 真实仪表：未连接、未验证

## 结论

Beta 2 的源码自动测试、独立扩展仓库测试、源码端到端流程和 Windows 干净目录发布包均
通过仿真验证。它修复了 Beta 1 之后 Measurement Module 长测量期间无法获取新温场
快照、Pause 计时和 Stop 协作退出的能力缺口，并增加分级 HTTP 报警和 Lake Shore
372A Beta 模块。

当前候选适合作为未签名 GitHub Prerelease，供继续开发和低风险台架验证；它不是稳定版，
也不是任何真实仪表的安全认证版本。接入传感器、磁体或有源输出前，仍必须完成本文末尾的
真机门槛。

## 自动测试

最终完整测试使用 Warning 当错误运行：

| 仓库 | 结果 |
|---|---:|
| OpenLab Control 核心 | 157/157 通过 |
| OpenLab Measurement Modules | 18/18 通过 |
| OpenLab Device Plugins | 1/1 通过 |

提交前定向测试：

- 核心版本契约、仓库模板和模块生命周期：25/25 通过；
- Lake Shore 372A backend、Frontend、清单和离线安装：17/17 通过；
- Windows 外置集成资源发布契约：4/4 通过。

附加检查：

- `compileall -q src tests tools`：通过；
- `pip check`：`No broken requirements found`；
- `git diff --check`：通过；
- PyInstaller 构建成功；缺失模块报告只包含 Windows 上预期的 POSIX、平台条件或可选
  导入，打包入口和实际运行均通过。

覆盖重点：

- SEQ 任意嵌套、List、Call、Pause 时钟冻结、Stop/取消/Hold 和运行时参数防绕过；
- primary/secondary/monitor 角色、手动/SEQ 限制同源和 Run 新鲜读回预检；
- 每设备独立进程、阻塞强杀、60 秒读链路恢复、恢复核对、写超时不重放和退出清理；
- 模块实时快照、可中断 pause/dwell、并行多模块、多行结果、IPC 超时和 worker 回收；
- Warning/Error 锁存、恢复、去重和异步报警重试；
- DAT 动态列、稀疏多行、追加 Schema、运行快照、日志和 Data Browser 自动刷新；
- 1080p/2K/4K 缩放、浮动窗口、窗口重建、信号销毁和长路径布局；
- 外部扩展内容指纹、API/core 约束、精确哈希 lock、离线 wheel 和依赖隔离。

## Lake Shore 372A

模块仓库完整测试为 18/18 通过。随模块携带的 PyVISA 1.16.2 和
typing_extensions 4.16.0 wheels 已核对 SHA-256，并通过核心实际
`--no-index --only-binary=:all: --require-hashes` 安装路径。

对用户提供的 Lake Shore Model 372 手册第 6 章进行了文本和页面渲染复核：

- `FREQ/FREQ?`、`FILTER/FILTER?`、`INSET/INSET?`、`INTYPE/INTYPE?`；
- `SCAN/SCAN?`、`QRDG?`、`RDGPWR?`、`RDGR?`、`RDGST?`；
- 激励频率、激励/电阻量程、分流语义和 8 个读数状态位。

实现中的 `CS OVL` 对应 `OVER_COMPLIANCE`，其他非零 `RDGST?` 位对应
`OVER_RANGE`，零对应 `NORMAL`。设置窗口已在 1.0x 和 2.0x/4K 布局下人工查看，
没有水平裁切。

这些检查只证明实现与手册和仿真响应一致。尚未验证 GPIB 控制器、VISA 实现、仪表固件、
可选扫描器型号、真实读数、分流时序或传感器安全。

## 报警报告

- 发射端使用有界后台队列，不阻塞 SEQ 或仪表安全动作；
- Warning 与 Error 使用稳定事件 ID 去重和重试；
- 请求不携带 QQ 号，收件人只由接收端配置；
- Warning 只发测试员；Error 发管理员和测试员；
- Token 缺失时 fail-closed，远程明文 HTTP 默认拒绝；
- 最终网络失败只产生本地 Warning，不把报警网络当成仪表互锁。

NoneBot2 OneBot V11 接收端源码、Token 验证、服务端路由和部分发送去重已自动测试。
真实 NoneBot、OneBot、QQ 和跨主机 HTTPS 尚未联调。

## 源码端到端

`examples/nested_scan.seq`：

- 退出码 0，最终 Completed；
- 任意嵌套温度/磁场/时间扫描执行完成；
- 无 Enabled 模块的 Warning 只提醒一次且 SEQ 继续；
- DAT 9 行，`BYAPP,OpenLab Control,0.11.0b2`；
- 温度、磁场保持最终当前状态。

## Windows 包

本地 onedir 候选：

- `OpenLabControl.exe` 为 3,052,678 字节；
- FileVersion / ProductVersion 均为 `0.11.0b2`；
- Authenticode：`NotSigned`；
- 配置、文档、示例、模块/设备目录、扩展模板和报警接收端位于 EXE 旁；
- 这些可写或外置资源未在 `_internal` 重复。

把整个目录复制到不含源码和开发 `PYTHONPATH` 的干净位置后：

| 场景 | 结果 |
|---|---|
| 默认无模块 headless | 退出码 0，Completed，9 行 DAT |
| 离屏 GUI 截图与关闭 | 退出码 0，1480 × 900，人工查看无裁切 |
| 手动复制并预置信任 `simulated_transport` | 退出码 0，12 行/14 列，2 个模块快照 |
| 报警接收端发布资源 | 文件存在，源码文档路径有效 |
| 退出后普通进程检查 | 无残留 `OpenLabControl.exe` |

正式发布归档的 ZIP 与 EXE SHA-256 应写入包旁的 `SHA256SUMS.txt`，避免把哈希写回包内
后改变自身结果。

## 未验证与真实仪表门槛

- 任何真实 GPIB/VISA、串口、以太网、厂商 SDK 或 Lake Shore 372/372A；
- 真实温控仪、磁体电源和测量仪表的量程、极性、速率、互锁与强杀后的物理状态；
- 断电、磁盘写满、网络抖动、驱动/固件差异和 8–24 小时真机长测；
- 报警接收端实际 OneBot/QQ 网络；
- Authenticode 签名与 SmartScreen 信任。

接入真实仪表前必须逐项完成 [TEST_PLAN.md](TEST_PLAN.md) 的上线清单。首次测试必须从
只读身份和状态查询开始，再进入最小安全激励，并准备独立于软件的硬件限值、互锁、急停
和人工恢复流程。软件分流命令、进程隔离、超时和 Hold 尝试不能替代这些措施。
