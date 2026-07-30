# OpenLab Control 0.11.3 验证报告

- 验证日期：2026-07-29
- 验证平台：Windows 11 x64
- 目标版本：v0.11.3 / PEP 440 `0.11.3`
- 真实仪表：未连接、未验证
- 验证结论：源码回归、Windows 冻结包、首次模块 Enable、SEQ 和进程退出测试通过

## 发布范围

0.11.3 是 0.11.2 的稳定补丁版本，包含：

- 修复首次批准模块信任后，运行时信任缓存尚未刷新、Enable 可能停在 Initializing 的问题；
- 解耦模块状态轮询间隔和 IPC 超时预算，并确保所有 Enable 失败路径回收 worker；
- 全局关闭数值框及未展开下拉框的滚轮改值，同时保留页面滚动和展开列表翻页；
- Temperature List Scan 使用方括号并保留每个点的输入精度，旧语法继续兼容；
- 文档不再写入具体私有 Device Plugin 仓库信息。

## 自动测试

三个仓库均使用 `-W error`，任何 Python Warning 都会使测试失败：

| 仓库 | 结果 |
| --- | ---: |
| OpenLab Control 核心 | 191/191 |
| Measurement Modules | 38/38 |
| Device Plugins | 1/1 |
| 合计 | 230/230 |

覆盖范围包括首次信任与 Enable、失败清理、模块 IPC 与轮询预算、滚轮输入策略、展开
下拉列表翻页、Temperature List 新旧语法、输入精度、SEQ 执行、设备与模块生命周期、
并行和多行测量、DAT/日志、GUI 和发布契约。此外，`compileall`、`git diff --check`
和 `pip check` 均通过。

## Windows 发布包验收

- `build.bat` 先执行 191 项测试，再由 PyInstaller 6.21.0 完成全新构建；
- `OpenLabControl.exe` 的 `FileVersion` 和 `ProductVersion` 均为 0.11.3；
- 冻结包中的共享依赖元数据为 PySide6 6.11.1、QtAwesome 1.4.2、
  packaging 26.2、PyVISA 1.16.2 和 typing_extensions 4.16.0；
- 发布目录没有 `__pycache__`、测试缓存、`.pyc` 或 `.pyo`；
- 在新复制的干净目录运行默认 nested scan，以退出码 0 完成并写入带 0.11.3
  `BYAPP` 标记的 DAT、设备状态日志和运行快照；
- 新方括号 Temperature List 示例可由冻结包读取并执行；
- 1480 × 900 离屏 GUI 以退出码 0 截图并关闭；
- 打包目录及最终 ZIP 解压目录均完成启动和关闭验收，没有残留 OpenLab Control 进程；
- 外置 `configs/`、`examples/`、`docs/`、`plugin_templates/`、`integrations/`
  和空扩展目录完整。

PyInstaller 只报告跨平台或可选导入缺失，例如 POSIX 模块、PyVISA 可选 NumPy 和旧版
prettytable 兼容导入；没有发现阻止当前 Windows 路径运行的缺失依赖。发布 ZIP、源码
归档和 `SHA256SUMS-v0.11.3.txt` 的校验值在上传前重新计算。

## 真实仪表边界

核心框架和仿真流程作为稳定版本发布，但本次仍未连接真实温控仪、磁场电源、GPIB
控制器或 Lake Shore 372A。Lake Shore 372A 继续保持模块自身的 Beta 标记。接入样品前
必须完成只读身份查询、最小安全激励、上下限、超时、Stop/Error/断线和硬件互锁验收。
