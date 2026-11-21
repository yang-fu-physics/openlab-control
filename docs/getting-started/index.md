# 快速上手路线图

第一次接触 OpenLab Control 时，建议**先不接任何真实仪表**，花 5 分钟用内置仿真跑通一次完整流程，弄清软件怎么工作，再连接真实硬件或开发自己的模块。

## 选择你的入口

| 你现在想做什么 | 推荐页面 | 适合谁 |
| --- | --- | --- |
| 直接在实验室电脑上安装并运行软件 | [使用 Windows 发布包](windows.md) | 实验员、日常使用者 |
| 从源码运行、改代码或跑单元测试 | [搭建源码环境](source.md) | 开发者 |
| 亲手新建并跑通第一条自动扫描序列 | [运行第一条 SEQ](first-sequence.md) | 所有人 |
| 把真实的 GPIB / USB / 网口仪表接进来 | [扫描与配置仪表](../guides/instrument-scanner.md) | 实验室管理员 |

## 5 分钟自检清单

完成下面这几步，你就掌握了 OpenLab Control 的核心操作：

- [ ] 启动软件（全新安装时底部没有任何仪表面板，这是正常的）。
- [ ] 用 **Instrument Scanner** 勾选内置仿真温度、磁场和二级冷头，重启后看到底部实时读数。
- [ ] 在工具栏点击 **Modules**，勾选启用自带的 `Simulated Transport` 示例模块。
- [ ] 打开 `examples/nested_scan.seq` 并点击 **Run**，观察左侧进度与模块监视卡。
- [ ] 在 `runs/` 下找到刚生成的实验文件夹，并用 **Data Browser** 打开其中的 `experiment.dat` 画图。

## 准备接入自己的设备？先分清该写哪一类

不要按仪表品牌区分，按**它在实验里承担的角色**选：

| 你希望这台仪表做什么 | 应该选哪类 | 配置方式 |
| --- | --- | --- |
| 响应标准 `Set / Scan Temperature` 或 `Set / Scan Field` 指令 | **System Instrument** | 控制器面板角色设为 `sample_temp` 或 `field`（全局各最多 1 个） |
| 只在底部面板手动设温/设场，不参与标准温场 SEQ | **System Instrument** | 控制器面板角色设为 `none` |
| 常驻显示冷头温度、液位、气压或提供简单开关按钮 | **System Instrument** | 只读面板或开关面板，角色为 `none` |
| 在 SEQ 执行到 `Measure` 时采集电阻、电压、电流等实验数据 | **Measurement Module** | 放入 `modules/`，实验前手动 Enable |

> 同一型号仪表在不同实验台上可能扮演不同角色：主温控仪放进 System Instrument 并设为 `sample_temp`；只在某次实验里顺带读数据的辅助表则可以做成 Measurement Module。

## 接下来去哪？

- **日常做实验**：按顺序看 [Windows 发布包](windows.md) → [第一条 SEQ](first-sequence.md) → [扫描与配置仪表](../guides/instrument-scanner.md) → [操作手册](../OPERATIONS.md)。
- **写测量模块**：直接进入 [Measurement Module 教程](../development/index.md)。
- **写温控仪 / 磁体驱动**：直接进入 [System Instrument 教程](../development/system-instrument.md)，不必先看测量模块章节。
- **准备接真机前**：务必核对 [真实仪表安全清单](../guides/safety-checklist.md)。
