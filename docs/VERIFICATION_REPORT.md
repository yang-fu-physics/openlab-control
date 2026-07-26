# OpenLab Control 0.11.1 验证报告

- 验证日期：2026-07-26
- 验证平台：Windows 11 x64
- 目标版本：v0.11.1 / PEP 440 `0.11.1`
- 真实仪表：未连接、未验证
- 验证结论：源码回归、Windows 冻结包、共享依赖导入和进程退出测试通过

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

## 自动测试

三个仓库均使用 `-W error`，任何 Python Warning 都会使测试失败：

| 仓库 | 结果 |
| --- | ---: |
| OpenLab Control 核心 | 162/162 |
| Measurement Modules | 17/17 |
| Device Plugins | 1/1 |
| 合计 | 180/180 |

其中包括依赖划分、manifest fail-closed、核心版本不兼容拦截、按钮显示、离线额外依赖
runtime、模块 IPC、并行和多行测量、SEQ、DAT、设备生命周期、报警、UI 和发布契约。
此外，`compileall`、`git diff --check` 和 `pip check` 均通过。

## 共享依赖验证

构建环境与冻结包中的 distribution metadata 均为：

| 依赖 | 共享版本 |
| --- | --- |
| PySide6 | 6.11.1 |
| QtAwesome | 1.4.2 |
| packaging | 26.2 |
| PyVISA | 1.16.2 |
| typing_extensions | 4.16.0 |

- 构建环境的 `pip check` 报告 `No broken requirements found`；
- PyVISA 和 typing_extensions 只从本地已核验 wheel 安装到构建环境，没有网络回退；
- 测试模块在实际冻结 worker 中动态导入 PyVISA，并核对版本必须等于 1.16.2；
- 该 worker 完成测量后 `plugin_runtime` 文件数为 0，证明使用的是主框架版本；
- 框架共享依赖不兼容时，模块会在导入其 Python 源码前被拒绝；
- 只有真正的额外依赖才显示 `Install Dependencies`。

## 源码端到端验证

- 任意嵌套 Scan 示例正常完成，DAT 共 9 行，`BYAPP` 版本为 0.11.1；
- 模块启用、SEQ 运行、动态列、多行结果、运行快照和有界关闭路径正常；
- 测试完成后没有残留 OpenLab Control 或模块 worker 进程。

## Windows 干净目录验证

- PyInstaller 构建完成，`FileVersion` 与 `ProductVersion` 均为 0.11.1；
- 默认设备的 headless 嵌套序列正常完成并写入 9 行；
- Simulated Transport 在 3 个 Measure 点各返回 R1–R4 四行，共写入 12 行、14 列；
- 上述模块运行保存了设置和启动状态两个快照文件；
- 把 Lake Shore 372A 0.1.0b3 直接复制到干净包后，模块描述符有效、共享依赖解析成功，
  Enable 不创建私有 runtime；未 Apply Settings 时拒绝开始测量，符合既定安全设计；
- 1480 × 900 GUI 启动、模块列表显示和关闭通过，没有发现裁切或残留进程；
- 所有 headless、模块 worker 和 GUI 验证的进程退出码均为 0；预期安全拒绝用例除外；
- EXE 当前未进行 Authenticode 签名；发布资产的最终 SHA-256 单列在
  `SHA256SUMS-v0.11.1.txt`。

PyInstaller 只报告平台或可选路径缺失，例如 PyVISA 可选的 NumPy、旧版 prettytable
兼容导入和非 Windows 模块；没有发现会阻止当前 Windows 路径运行的缺失依赖。

## 真实仪表边界

Lake Shore 372A 仍是 Beta 硬件支持。本次只验证依赖可用性、进程生命周期、协议命令生成、
读回核对、异常分流和仿真数据流程，没有连接真实 GPIB 控制器、VISA 厂商驱动或仪表。
接入样品前仍必须执行只读身份查询、最小安全激励、Stop/Error/断线和硬件互锁验收。
