# OpenLab Control 0.11.4 验证报告

- 验证日期：2026-07-31
- 验证平台：Windows 11 x64
- 目标版本：v0.11.4 / PEP 440 `0.11.4`
- 发布结论：核心框架可作为稳定版本发布；真实仪表模块继续保持 Beta
- 真实仪表：Lake Shore 372A、LR-700、6221、2182A、7001 和 3706A 均未连接、未验证

## 发布范围

- Measurement Module 的正式 DAT 状态统一为非负整数 `StatusCode`；`0` 固定表示正常，
  其他代码和优先级由模块定义。非零状态行的当前测量结果留空，可读告警只写界面和事件
  日志。
- 模块可以为正式结果行附带最多 32,768 个有限原始数值。核心按“正式 DAT + 模块”写入
  无表头 `rawdata` sidecar，并处理同名 DAT、追加和 `create` 重建。
- Stop 恰好发生在模块 `begin_sequence`/ARM 等待期间时，保持正常 Stop 控制流，不会把
  模块误标为 Faulted。
- Measurement Modules 仓库新增 Keithley 6221 + 2182A Delta + 3706A Beta 模块；既有
  7001 方案同步采用稳定核心 0.11.4 的 rawdata/状态契约。两种方案均支持共享 Armed 和
  逐通道重新 ARM 模式。

## 自动验证

三个仓库均使用 `-W error`，任何 Python Warning 都会使测试失败：

| 仓库 | 结果 |
| --- | ---: |
| OpenLab Control 核心 | 195/195 |
| Measurement Modules | 82/82 |
| Device Plugin 示例 | 1/1 |
| 合计 | 278/278 |

专项测试覆盖 rawdata 的 IPC 验证、写入、DAT 切换和重建，并行模块 sidecar，数值状态码，
故障稀疏行，ARM 后 3 秒等待，共享/逐通道 ARM，7001 缺失降级，3706A TSP 路由与闭合
回读，运行期切换失败立即中止，Stop/通信异常安全收尾，异常读数 Warning 继续和 SI 前缀
输入。完整测试还覆盖既有 SEQ、设备与模块生命周期、DAT/Data Browser、GUI、报警和扩展
依赖契约。

此外，三个仓库的 `compileall`、`git diff --check` 均通过；核心 `pip check` 无损坏依赖，
4 项发布契约测试通过。

## Windows 发布包验收

- `build.bat` 再次执行 195 项核心测试，并由 PyInstaller 完成全新构建；
- `OpenLabControl.exe` 的 `FileVersion` 和 `ProductVersion` 均为 0.11.4；
- 冻结包共享依赖元数据为 PySide6 6.11.1、QtAwesome 1.4.2、packaging 26.2、
  PyVISA 1.16.2 和 typing_extensions 4.16.0；
- 发布目录共 336 个文件、约 132.61 MiB，未发现 `__pycache__`、测试缓存、`.pyc`
  或 `.pyo`；
- 源码和复制后的冻结包 GUI 均以退出码 0 完成 1480 x 900 离屏截图并自动关闭，截图经
  人工检查未见布局截断或异常窗口；
- 复制后的冻结包运行默认 nested scan，以退出码 0 完成并写入 0.11.4 `BYAPP`、
  `device_status.dat`、事件日志和运行快照；
- 手动复制并预置信任 `simulated_transport` 1.0.2 后，冻结包模块 SEQ 以退出码 0 完成，
  写出 12 行/13 列稀疏 DAT、模块设置和起始状态快照；
- 所有冒烟进程均正常退出，没有残留 OpenLab Control 进程。

PyInstaller 只报告跨平台或可选导入缺失，例如 POSIX 模块、PyVISA 可选 NumPy 和旧版
prettytable 兼容导入；没有发现阻止当前 Windows 路径运行的缺失依赖。

## 真实仪表边界

核心稳定发布不代表硬件方案已经通过真机验证。接入样品前，必须逐台完成身份查询、最小
安全激励、量程与 compliance、上下限、超时、Stop/Error/断线、开关路由回读及硬件互锁
验收。7001 `routing.toml` 默认按 Slot 1 地址解释；3706A 默认使用 Slot 1 的 4 线闭合
路由。两者都必须与实际卡型、插槽和接线逐项核对。

软件默认 100 µA / 10 V 上限只是待现场复核的保守占位值，不是硬件安全认证。未经
真机验收时，不应把任何硬件 Beta 模块直接用于无人值守实验。
