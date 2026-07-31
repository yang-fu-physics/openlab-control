# OpenLab Control 0.11.5 验证报告

- 验证日期：2026-08-01
- 验证平台：Windows 11 x64
- 目标版本：v0.11.5 / PEP 440 `0.11.5`
- 当前结论：三个源码仓库自动验证和 Windows 打包验收全部通过；核心可作为稳定版发布，
  真实仪表模块继续保持 Beta
- 真实仪表：Lake Shore 372A、LR-700、6221、2182A、7001、3706A、2400、6517B 和
  2614B 均未连接、未验证

## 发布范围

- Measurement Module API 1.1 增加显式 `aligned_slots` / `once_per_slot` 调度契约；缺省
  显示 Warning 并按 `once_per_slot` 兼容处理。
- 一次 `T Measure` 按扫描槽位并集写“每个逻辑通道一行”。同槽位模块并行合入该行，
  单次模块每行重新测量，Disabled 扫描槽位保持空列。
- 单次 backend 调用严格只允许一行；无行、多行以及 emit 后又 return 都会 Error。
- 模块原始数据继续按正式 DAT + 模块写入独立 sidecar；同槽位多模块共享正式行但不共享
  rawdata 文件。
- Measurement Modules 仓库同步迁移 372A、LR-700、两种 Delta 和示例模块，并新增
  `once_per_slot` 的 2400、6517B、2614B Beta 模块。主动输出模块默认每行关闭输出，
  可选在成功行之间保持；所有结束和异常路径仍强制恢复安全状态。

## 自动验证

三个仓库均使用 `-W error`，任何 Python Warning 都会使测试失败：

| 仓库 | 结果 |
| --- | ---: |
| OpenLab Control 核心 | 201/201 |
| Measurement Modules | 133/133 |
| Device Plugin 示例 | 1/1 |
| 合计 | 335/335 |

专项测试覆盖 mode 缺省 Warning、槽位并集、CH1+CH1 对齐、Disabled 槽位空列、单次模块
逐行重复、同槽位 DAT 合并、rawdata sidecar、严格单行契约，以及既有 ARM、切换路由、
Stop/通信异常安全收尾、数据 Warning 和整数状态码。完整测试还覆盖 SEQ、设备/模块
生命周期、DAT/Data Browser、GUI、报警和扩展依赖契约。

此外，三个仓库的 `compileall`、`git diff --check` 均通过；核心 `pip check` 无损坏依赖，
4 项发布契约测试通过。

## Windows 发布包验收

- `build.bat` 完整重建成功；构建过程再次运行核心 201/201 测试并通过。
- `OpenLabControl.exe` 的 FileVersion 与 ProductVersion 均为 `0.11.5`；未发现
  `__pycache__`、`.pyc`、`.pyo` 或重复的 `_internal` 资源目录。
- 解包目录共 336 个文件、139,073,045 bytes（132.63 MiB）。PyInstaller 缺失模块报告
  仅包含 Windows 不使用的 POSIX 模块和可选依赖；实际冻结启动未出现缺包错误。
- 源码版与冻结版 `--gui-smoke` 均退出 0，生成的 1480×900 主窗口截图均为
  105,527 bytes；菜单、SEQ 树、命令栏和三个设备状态块完整可读。
- 源码版与冻结版 `nested_scan.seq` 无界面运行均 Completed、退出 0；嵌套温度/磁场/时间
  扫描和 Disabled 模块 Warning 路径正常，结束后无残留 `OpenLabControl` 进程。
- 在冻结包副本中复制并信任 API 1.1 `simulated_transport` 后，模块在独立进程中 Enable，
  `module_measurement.seq` Completed、退出 0。3 个 Measure 各写 4 个逻辑通道行，共 12 行；
  每行恰有一个 R1–R4 值且 `StatusCode=0`，未发现无行、多行或跨槽位污染，退出后无残留
  模块/主程序进程。
- 模块 Manager、Settings 和 Status 三张离屏预览均成功生成；窗口初始宽度无横向滚动条，
  Apply Settings 仍只出现在 Settings 页。

最终发布 ZIP 的重新解压和哈希核验将在生成发布资产后执行，并单独记录在随 Release 上传的
验证报告中。

## 真实仪表边界

核心稳定发布不代表硬件方案已经通过真机验证。接入样品前，必须逐台完成身份查询、最小
安全激励、量程与 compliance、上下限、超时、Stop/Error/断线、开关路由回读及硬件互锁
验收。7001 `routing.toml` 默认按 Slot 1 地址解释；3706A 默认使用 Slot 1 的 4 线闭合
路由。两者都必须与实际卡型、插槽和接线逐项核对。

软件默认 100 µA / 10 V 上限只是待现场复核的保守占位值，不是硬件安全认证。未经
真机验收时，不应把任何硬件 Beta 模块直接用于无人值守实验。
