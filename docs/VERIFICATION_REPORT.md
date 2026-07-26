# OpenLab Control 0.11.1 验证报告（候选）

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64
- 目标版本：v0.11.1 / PEP 440 `0.11.1`
- 真实仪表：未连接、未验证
- 当前状态：分项测试已通过，等待完整回归和最终 Windows 干净目录打包验收

## 修复范围

0.11.1 修复 0.11.0 Windows 包把 PyVISA 视为 Lake Shore 372A 私有依赖、但打包版又没有
携带额外安装用 Python，导致模块首次使用时无法准备 runtime 的发布缺陷。

新依赖模型如下：

- PySide6 6.11.1、QtAwesome 1.4.2、packaging 26.2、PyVISA 1.16.2 和
  typing_extensions 4.16.0 由主框架精确锁定并直接供全部模块/设备插件使用；
- manifest 仍可声明这些包的兼容范围，核心在导入扩展源码前核对；
- 兼容的框架依赖不创建 `plugin_runtime`，也不显示 Install Dependencies；
- 只有框架未提供的额外依赖才继续使用本地 wheel、带 SHA-256 的 lock 和按内容指纹
  隔离的 runtime；
- Lake Shore 372A 0.1.0b3 删除重复 wheel，并要求 OpenLab Control 0.11.1。

## 已完成的分项验证

- 核心依赖划分、manifest fail-closed、按钮显示和发布契约定向测试：37/37 通过；
- Lake Shore 372A 与模拟模块完整仓库测试：17/17 通过；
- `compileall` 与 `git diff --check`：通过；
- PyVISA/typing_extensions 仅从本地已核验 wheel 安装到构建环境，无网络回退。

## 最终验收待补录

正式发布前必须把以下实测结果补入本报告：

- 三个仓库使用 `-W error` 的完整测试总数；
- `pip check` 和精确依赖版本；
- 源码 nested SEQ 与模块多行测量；
- PyInstaller 构建、EXE 版本、GUI/进程退出；
- 干净目录中直接复制 372A 后不创建 `plugin_runtime` 即可 Enable；
- 冻结包内 PyVISA 版本和模块 worker 动态导入；
- 发布 ZIP、源码包和 SHA-256。

## 真实仪表边界

Lake Shore 372A 仍是 Beta 硬件支持。本次只验证依赖可用性、进程生命周期、协议命令生成、
读回核对、异常分流和仿真数据流程，没有连接真实 GPIB 控制器、VISA 厂商驱动或仪表。
接入样品前仍必须执行只读身份查询、最小安全激励、Stop/Error/断线和硬件互锁验收。
