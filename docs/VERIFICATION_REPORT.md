# OpenLab Control 0.11.0 验证报告

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64（build 26200）
- 版本名称：v0.11.0 / PEP 440 `0.11.0`
- 发布状态：本地稳定候选；最终复测和打包正在进行
- 真实仪表：未连接、未验证

## 当前结论

0.11.0 稳定候选以 0.11.0 Beta 2 的已验证实现为基线，不引入新的运行行为。稳定范围是
核心框架、扩展 API、仿真设备和发布契约；Lake Shore 372A 等没有经过真机验证的硬件
扩展仍保留独立 Beta 状态。最终源码、Windows 干净包和发布资产复核完成前，不得把本
报告视为正式发布证明。

## 本轮验证范围

- 核心 0.11.0 版本、文档、DAT `BYAPP` 和 Windows 版本资源；
- SEQ、设备/模块生命周期、实时快照、Pause/Stop、报警和资源回收完整回归；
- DAT 动态列、稀疏多行结果、日志、运行快照与 Data Browser；
- 独立 Device Plugin 和 Measurement Module 仓库兼容性；
- 源码与 Windows 干净目录的 headless、GUI 和模块端到端流程；
- 发布 ZIP、EXE 与 GitHub 资产 SHA-256。

## 稳定与硬件边界

“稳定版本”不表示真实仪表安全认证。默认配置仍全部使用仿真设备；任何真实温控仪、磁体
电源、GPIB/VISA 仪表和 OneBot/QQ 网络仍需按 [TEST_PLAN.md](TEST_PLAN.md) 分阶段验证。
软件超时、进程隔离、Hold 或分流命令不能替代硬件限值、互锁、急停和人工恢复流程。
