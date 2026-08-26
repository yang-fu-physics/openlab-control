# 第一次使用从这里开始

先在不连接真实仪表的情况下运行一次程序。确认基本操作后，再选择是否学习模块开发。

## 选择运行方式

| 目标 | 建议入口 | 适合谁 |
| --- | --- | --- |
| 直接体验界面和 SEQ | [Windows 发布包](windows.md) | 使用者、实验员 |
| 写测量模块或修改程序 | [源码环境](source.md) | 开发者 |
| 理解运行流程 | [第一条 SEQ](first-sequence.md) | 所有人 |

## 五分钟检查点

- [ ] 全新安装在没有 System Instrument 面板时也能启动。
- [ ] 所有 Measurement Module 启动时都是 Disabled。
- [ ] 需要运行温场示例时，能先在 Instrument Scanner 最后一页明确启用对应仿真。
- [ ] 启用仿真后，能打开 `examples/nested_scan.seq` 并在无真实硬件时运行。
- [ ] 每次 Run 在 `runs/` 下产生独立目录、DAT、事件和仪表状态日志。
- [ ] Stop 后温度和磁场继续仪表原有动作，框架没有发送 Set 或 Hold。

!!! info "先判断要接入哪一类"

    **Measurement Module** 用来测量电阻、电压、电流等数据。

    **System Instrument** 只用来更换主温度、主磁场或只读监视仪表。普通测量模块不需要它。

## 按职责选择接入方式

不要只看仪表型号；先看它在 OpenLab Control 中负责什么。

| 你希望它做什么 | 应写什么 |
| --- | --- |
| 接受标准温度/磁场 SEQ | System Instrument controller 面板，角色为 `sample_temp` 或 `field` |
| 只供手动控制，不接管标准温场 SEQ | System Instrument controller 面板，角色为 `none` |
| 显示 2nd Stage 等一般辅助量 | System Instrument 只读面板，角色为 `none` |
| 在 `Measure` 时产生电阻、电压、电流等实验列 | Measurement Module |

同一型号也可能承担不同职责。例如一台温控仪若负责标准样品温度控制，就把它的 controller
面板角色设为 `sample_temp`；另一台温控仪可以保持 `none`，只显示或手动控制。若仪表只在
某个实验方案里读取传感器并写测量列，也可以属于 Measurement Module。角色为 `none` 的
面板不参与标准温度/磁场 SEQ。

## 推荐学习顺序

1. 运行一次嵌套扫描，观察 Sequence Editor、状态卡和运行目录。
2. 按 [Windows 安装说明](windows.md) 把 `simulated_transport` 复制到
   `modules/`；源码环境也使用相同目录。
3. 重启，确认模块出现在 Modules 中，再练习 Enable 和 Disable。
4. 跟随 [第一个测量模块](../development/first-module.md) 完成四通道教程模块。
5. 根据需要增加设置窗口或模块自己的 SEQ 指令。
6. 在连接真实仪表前完成 [安全清单](../guides/safety-checklist.md)。

如果你的目标只是更换温控仪或磁场控制器，请直接阅读
[编写 System Instrument](../development/system-instrument.md)，不要从测量模块教程开始。
