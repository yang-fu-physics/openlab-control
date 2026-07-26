# OpenLab Control 0.11.0b2 验证报告

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64
- 版本名称：v0.11.0 Beta 2 / PEP 440 `0.11.0b2`
- 发布状态：本地候选；尚未推送、打标签或发布
- 真实仪表：未连接、未验证

## 当前结论

Beta 2 修复了 Beta 1 之后 Measurement Module 长测量期间无法获取新温场快照、Pause
计时和 Stop 协作退出的能力缺口，并增加分级 HTTP 报警。源码、扩展仓库和 Windows
发布包的最终复测仍在进行；完成前不得把本报告视为已发布证明。

## 本轮验证范围

- 实时模块上下文、跨进程快照请求、Pause/Stop 和 worker 回收；
- Warning/Error 分级报警、Token、重试、去重及本地失败状态；
- 核心空模块目录和更新后的 Measurement Module 仓库模板；
- Lake Shore 372A 模块的离线依赖、GPIB 仿真、设置回读、稀疏多行结果与分流路径；
- 完整自动测试、嵌套 SEQ、DAT、GUI/4K 和 Windows onedir 包。

## 暂未验证

- 任何真实 GPIB/VISA 通信或 Lake Shore 372/372A；
- 真实温控仪、磁体电源和测量仪表的硬件互锁、安全状态与强杀结果；
- 报警接收端实际 OneBot/QQ 网络；
- Authenticode 签名与 SmartScreen 信任。

接入真实仪表前仍必须完成 [TEST_PLAN.md](TEST_PLAN.md) 的上线清单。软件分流命令、进程
隔离、超时和 Hold 尝试不能替代硬件限值、互锁、急停和人工恢复流程。
