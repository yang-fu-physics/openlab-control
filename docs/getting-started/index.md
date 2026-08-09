# 第一次使用从这里开始

先在不连接真实仪表的情况下运行一次程序。确认基本操作后，再选择是否学习模块开发。

## 选择运行方式

| 目标 | 建议入口 | 适合谁 |
| --- | --- | --- |
| 直接体验界面和 SEQ | [Windows 发布包](windows.md) | 使用者、实验员 |
| 写测量模块或修改程序 | [源码环境](source.md) | 开发者 |
| 理解运行流程 | [第一条 SEQ](first-sequence.md) | 所有人 |

## 五分钟检查点

- [ ] 程序以仿真温度、仿真磁场和只读 2nd Stage 启动。
- [ ] 所有 Measurement Module 启动时都是 Disabled。
- [ ] 能打开 `examples/nested_scan.seq` 并在无真实硬件时运行。
- [ ] 每次 Run 在 `runs/` 下产生独立目录、DAT、事件和设备状态日志。
- [ ] Stop 后温度和磁场保持当前状态，而不是自动归零。

!!! info "不要混淆两种插件"

    **Measurement Module** 用来测量电阻、电压、电流等数据。

    **Device Plugin** 只用来更换主温度、主磁场或只读监视设备。普通测量模块不需要它。

## 推荐学习顺序

1. 运行一次嵌套扫描，观察 Sequence Editor、状态卡和运行目录。
2. 打开 Modules，练习 Enable 和 Disable。
3. 跟随 [第一个测量模块](../development/first-module.md) 完成四通道教程模块。
4. 根据需要增加设置窗口或模块自己的 SEQ 指令。
5. 在连接真实仪表前完成 [安全清单](../guides/safety-checklist.md)。

如果你的目标只是更换温控仪或磁场控制器，请直接阅读
[更换温度或磁场设备](../development/device-plugin.md)，不要从测量模块教程开始。
