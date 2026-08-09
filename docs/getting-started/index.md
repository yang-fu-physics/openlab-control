# 快速开始路线图

这一部分先让程序在仿真环境中完整跑通，再进入扩展开发。不要把“程序能启动”等同于
“真实仪表已经安全”。

## 选择运行方式

| 目标 | 建议入口 | 适合谁 |
| --- | --- | --- |
| 直接体验界面和 SEQ | [Windows 发布包](windows.md) | 使用者、实验员 |
| 修改核心或开发扩展 | [源码环境](source.md) | 模块作者、设备插件作者 |
| 理解运行流程 | [第一条 SEQ](first-sequence.md) | 所有人 |

## 五分钟检查点

- [ ] 程序以仿真温度、仿真磁场和只读 2nd Stage 启动。
- [ ] 所有 Measurement Module 启动时都是 Disabled。
- [ ] 能打开 `examples/nested_scan.seq` 并在无真实硬件时运行。
- [ ] 每次 Run 在 `runs/` 下产生独立目录、DAT、事件和设备状态日志。
- [ ] Stop 后温度和磁场保持当前状态，而不是自动归零。

!!! info "两个扩展入口"

    Measurement Module 拥有一次完整测量所需的仪表和时序；Device Plugin 只负责温度、
    磁场或只读监视设备。不要用 Measurement Module 绕过核心温场限制。

## 推荐学习顺序

1. 运行一次嵌套扫描，观察 Sequence Editor、状态卡和运行目录。
2. 复制最小 `simulated_transport`，确认 Enable/Disable 生命周期。
3. 跟随 [第一个测量模块](../development/first-module.md) 完成四通道教程模块。
4. 再根据需要学习 QWidget、自定义 SEQ 指令或 Device Plugin。
5. 在连接真实仪表前完成 [安全清单](../guides/safety-checklist.md)。
