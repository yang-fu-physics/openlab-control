# OpenLab Control 0.11.0 验证报告

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64（build 26200）
- 源码运行时：Python 3.13.2、PySide6 6.11.1
- 打包工具：PyInstaller 6.21.0（onedir）
- 版本名称：v0.11.0 / PEP 440 `0.11.0`
- 发布结论：验证通过，可作为正式 GitHub Release
- 真实仪表：未连接、未验证

## 结论

0.11.0 以 0.11.0 Beta 2 的已验证实现为基线，不增加新的运行行为。核心源码自动测试、
独立扩展仓库测试、源码端到端流程、Windows 干净目录发布包和模拟模块完整生命周期均
通过验证。

本次“稳定”范围仅包括核心框架、扩展 API、仿真设备、仿真流程和发布契约。Lake Shore
372A 等没有经过真机验证的硬件扩展继续保留各自的 Beta 状态；稳定核心不等于真实仪表
安全认证，也不允许跳过上线清单。

## 自动测试

完整测试使用 `-W error`，把 Python Warning 视为测试失败：

| 仓库 | 结果 |
|---|---:|
| OpenLab Control 核心 | 157/157 通过 |
| OpenLab Measurement Modules | 18/18 通过 |
| OpenLab Device Plugins | 1/1 通过 |

稳定版版本、发布资源和扩展仓库模板定向测试为 8/8 通过。

附加检查：

- `compileall -q src tests tools`：通过；
- `pip check`：`No broken requirements found`；
- `git diff --check`：通过；
- PyInstaller 构建成功；
- PyInstaller 缺失模块报告已复核，Windows 条件导入、可选导入以及实际打包入口均由
  后续干净包场景覆盖。

完整回归覆盖的重点包括：

- SEQ 任意嵌套、List、Call、Pause 时钟冻结、Stop/取消/Hold 和运行时参数防绕过；
- primary/secondary/monitor 角色、手动/SEQ 限制同源和 Run 新鲜读回预检；
- 每设备独立进程、阻塞强杀、60 秒读链路恢复、恢复核对、写超时不重放和退出清理；
- 模块实时快照、可中断 pause/dwell、并行多模块、多行结果、IPC 超时和 worker 回收；
- Warning/Error 锁存、恢复、去重和异步分级报警重试；
- DAT 动态列、稀疏多行、追加 Schema、运行快照、日志和 Data Browser 自动刷新；
- 1080p/2K/4K 缩放、浮动窗口、窗口重建、信号销毁和长路径布局；
- 外部扩展内容指纹、API/core 约束、精确哈希 lock、离线 wheel 和依赖隔离。

## 独立 Measurement Module 与 Device Plugin

Measurement Module 仓库 18/18 通过，其中包括：

- `simulated_transport` 完整生命周期；
- Lake Shore 372A 初始化不自动连接或 Apply；
- GPIB 资源选择、设置读回核对和四通道逐行测量；
- pause/dwell 可中断等待、Stop 协作取消、异常时逐通道分流；
- 短暂读失败重连重试、重试耗尽后 Error、连接丢失时不伪报安全；
- 每个通道固定稀疏列、相角/电流/状态和两次温场快照平均；
- PyVISA 及其依赖的离线哈希 wheel 安装。

Device Plugin 私密仓库布局测试 1/1 通过。正式温控仪和磁体插件仍需在真实仓库中按
设备逐项实现协议、读回、设定和 Hold，并经过真实仪表验收。

## 源码端到端

运行 `examples/nested_scan.seq`：

- 退出码 0，最终状态 Completed；
- 任意嵌套温度、磁场和时间扫描执行完成；
- 没有 Enabled 模块时只报告一次 Warning，SEQ 继续；
- DAT 共 9 行，`BYAPP,OpenLab Control,0.11.0`；
- 结束后温度和磁场保持最终当前状态。

## Windows 发布包

本地 onedir 稳定候选：

- `OpenLabControl.exe` 为 3,052,676 字节；
- FileVersion / ProductVersion 均为 `0.11.0`；
- Authenticode：`NotSigned`；
- 配置、文档、示例、模块/设备目录、扩展模板和报警接收端位于 EXE 旁；
- 这些可写或外置资源没有在 `_internal` 中重复。

把整个目录复制到不含源码和开发 `PYTHONPATH` 的干净位置后：

| 场景 | 结果 |
|---|---|
| 默认无模块 headless | 退出码 0，Completed，9 行 DAT，版本 0.11.0 |
| 离屏 GUI 截图与关闭 | 退出码 0，1480 × 900，人工查看无裁切 |
| 手动复制并预置信任 `simulated_transport` | 退出码 0，12 行/14 列，2 个模块快照 |
| 报警接收端发布资源 | 文件存在，文档引用路径有效 |
| 退出后普通进程检查 | 无残留 `OpenLabControl.exe` |

正式发布资产在上传前后应分别核对 SHA-256；校验值放在独立的 `SHA256SUMS` 文件中，
不写回 ZIP 内部，避免改变被校验对象。

## 未验证与真实仪表门槛

以下内容不属于本次稳定验证：

- 任何真实 GPIB/VISA、串口、以太网、厂商 SDK 或 Lake Shore 372/372A；
- 真实温控仪、磁体电源和测量仪表的量程、极性、速率、互锁与强杀后的物理状态；
- 断电、磁盘写满、网络抖动、驱动/固件差异和 8–24 小时真机长测；
- 报警接收端实际 OneBot、QQ 和跨主机 HTTPS；
- Authenticode 签名与 SmartScreen 信任。

接入真实仪表前必须逐项完成 [TEST_PLAN.md](TEST_PLAN.md) 的上线清单。首次测试必须从
只读身份和状态查询开始，再进入最小安全激励，并准备独立于软件的硬件限值、互锁、急停
和人工恢复流程。软件分流命令、进程隔离、超时和 Hold 尝试不能替代这些措施。
