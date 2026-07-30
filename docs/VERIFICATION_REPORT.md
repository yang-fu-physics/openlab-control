# OpenLab Control 0.11.4.dev0 开发验证报告

- 验证日期：2026-07-29
- 验证平台：Windows 11 x64
- 目标版本：PEP 440 `0.11.4.dev0`
- 阶段：源码开发验证，未构建或发布 Windows 安装包
- 真实仪表：6221、2182A、7001 均未连接、未验证

## 本次开发范围

- Measurement Module 可把与正式结果行对应的有限原始数值序列交给核心；
- 核心按正式 DAT 和模块分别写无表头 `rawdata` sidecar，并处理同名 DAT 与
  `Set Datafile ... create` 重建；
- Stop 恰好发生在模块 `begin_sequence` 等待时，保持正常 Stop 控制流；
- Measurement Modules 仓库新增 Keithley 6221 + 2182A Delta + 可选 7001 Beta
  模块，四通道、共享 Armed 与逐通道重 ARM 两种模式。

## 自动验证

当前开发阶段执行：

| 仓库 | 结果 |
| --- | ---: |
| OpenLab Control 核心 | 194/194 |
| Measurement Modules | 51/51 |
| Device Plugin examples | 1/1 |
| 合计 | 246/246 |

专项测试覆盖 rawdata 验证/写入/切换、并行模块 sidecar、ARM 命令后 3 秒等待、
共享 Armed、逐通道重 ARM、7001 缺失降级、运行期切换失败无重试、Stop/通信异常即时
安全收尾、异常读数 Warning 继续以及 SI 前缀输入。完整测试还覆盖既有 SEQ、设备与
模块生命周期、DAT/Data Browser、GUI 和扩展依赖契约。

## 尚未完成

- 未运行本开发版本的 PyInstaller 构建、冻结包 GUI/headless 或归档校验；
- 未用真实 6221/2182A/7001、实际开关卡、DUT、线缆和 GPIB 控制器验证；
- 软件默认 100 µA / 10 V 上限只是待现场复核的保守占位值，不是硬件安全认证；
- 7001 `routing.toml` 默认按 Slot 1 地址解释，必须和实际卡型、插槽及接线逐项核对。

因此 `0.11.4.dev0` 目前不应作为稳定发行版，也不应直接用于无人值守真实仪表实验。
