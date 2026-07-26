# OpenLab Control 0.11.2 验证报告

- 验证日期：2026-07-27
- 验证平台：Windows 11 x64
- 目标版本：v0.11.2 / PEP 440 `0.11.2`
- 真实仪表：未连接、未验证
- 验证结论：源码回归、Windows 冻结包、Data Browser 实际文件和进程退出测试通过

## 发布范围

0.11.2 是 0.11.1 的稳定补丁版本，包含：

- 保存 SEQ 时生成受限的 `<序列名>.modules.toml`，再次 Load SEQ 时导入对应模块设置；
- 导入只更新 Settings 页，不自动 Enable、连接仪表或 Apply Settings；
- `Set Datafile` 使用 Windows 原生打开/保存窗口，并保护运行目录中的其他产物；
- 每个 Run 独立记录 `device_status.dat`，包含当前值、目标、速率、稳定和连接状态；
- Live Trend 使用设备采样时间并合并可见重绘，避免高频重绘影响设备轮询；
- Data Browser 把可确认来源的绝对时间戳显示为实际时间，并使用整齐主刻度。

## 自动测试

三个仓库均使用 `-W error`，任何 Python Warning 都会使测试失败：

| 仓库 | 结果 |
| --- | ---: |
| OpenLab Control 核心 | 182/182 |
| Measurement Modules | 17/17 |
| Device Plugins | 1/1 |
| 合计 | 200/200 |

覆盖范围包括 SEQ 解析和嵌套扫描、模块设置伴随文件、设备与模块进程生命周期、并行和
多行测量、IPC、DAT/事件/设备状态日志、报警、Data Browser、Live Trend、GUI 和发布契约。
此外，`compileall`、`git diff --check` 和 `pip check` 均通过。

## Windows 发布包验收

- `build.bat` 先执行 182 项测试，再由 PyInstaller 6.21.0 完成全新构建；
- `OpenLabControl.exe` 的 `FileVersion` 和 `ProductVersion` 均为 0.11.2；
- 冻结包中的共享依赖元数据为 PySide6 6.11.1、QtAwesome 1.4.2、
  packaging 26.2、PyVISA 1.16.2 和 typing_extensions 4.16.0；
- 发布目录没有 `__pycache__`、测试缓存、`.pyc` 或 `.pyo`；
- 在新复制的干净目录运行默认 nested scan，21.34 秒内以退出码 0 完成，实验 DAT
  写入 9 行、`device_status.dat` 写入 19 行，并包含 0.11.2 与明确 epoch 标记；
- 1480 × 900 离屏 GUI 在 4.06 秒内以退出码 0 截图并关闭；
- 打包版直接打开用户提供的 Quantum Design DAT，时间轴显示实际仪表时间，
  温度使用 0/50/100…、磁场使用 -60000/-40000… 等整齐刻度；
- 两次打包版验收前后均无残留 OpenLab Control 进程；
- 外置 `configs/`、`examples/`、`docs/`、`plugin_templates/`、`integrations/`
  和空扩展目录完整。

PyInstaller 只报告跨平台或可选导入缺失，例如 POSIX 模块、PyVISA 可选 NumPy 和旧版
prettytable 兼容导入；没有发现阻止当前 Windows 路径运行的缺失依赖。发布 ZIP、源码
归档和 `SHA256SUMS-v0.11.2.txt` 的校验值在上传前重新计算。

## 真实仪表边界

核心框架和仿真流程作为稳定版本发布，但本次仍未连接真实温控仪、磁场电源、GPIB
控制器或 Lake Shore 372A。Lake Shore 372A 继续保持模块自身的 Beta 标记。接入样品前
必须完成只读身份查询、最小安全激励、上下限、超时、Stop/Error/断线和硬件互锁验收。
