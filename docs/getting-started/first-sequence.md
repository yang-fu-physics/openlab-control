# 运行第一条 SEQ

这一页只使用仿真温度和磁场，不需要连接仪表。SEQ 是一棵可以缩进的命令树；扫描命令下面
可以继续放扫描或 `Measure`。

## 在界面中建立命令

1. 启动程序，确认底部 Temperature、Magnetic Field 和 2nd Stage 都有仿真读数。
2. 点击 **File → New**。编辑器中会保留最后一行 `End Sequence`。
3. 选中 `End Sequence`，在右侧 **Sequence Command Bar** 依次双击并确认：
   **Remark**、**Set Datafile**、**Set Temperature**、**Scan Field**。
4. `Remark` 的文字填写 `First simulated run`。`Set Datafile` 的 Mode 选择 `create`，
   Location 选择 `Run folder`，文件名填写
   `experiment.dat`。
5. `Set Temperature` 填 300 K、10 K/min、Settle。
6. `Scan Field` 填 0 Oe 到 200 Oe、3 points、5000 Oe/min、Settle。
7. 选中刚插入的 **Scan Field** 或它的 **End Scan**，双击 **Measure**。Measure 会缩进到
   Scan Field 里面。
8. 点击 **File → Save As**，把文件保存为 `first-run.seq`。

界面中应看到与下面等价的内容；缩进和 `End Scan` 由编辑器自动维护：

```text
T Remark First simulated run
T Set Datafile create experiment.dat
T Set Temperature 300 K at 10 K/min in Settle mode
T Scan Field 0 Oe to 200 Oe in 3 steps at 5000 Oe/min, Settle
T     Measure
T End Scan
T End Sequence
```

`T` 表示该命令启用；切换为 `F` 后该命令不会执行。`Settle` 表示到达目标并满足稳定条件后
再继续。这里使用明确的 `create`，不是需要替换的占位文字；默认的 `open|create` 表示文件
存在时追加、不存在时创建。

## 运行并检查结果

1. 点击主窗口左侧或工具栏的 **Run**。
2. 等待状态变为 **Completed**。
3. 打开 `runs/<时间>_first-run/`，应看到：

```text
sequence.seq
configuration.toml
module_settings/
experiment.dat
instrument_status.dat
events.dat
```

如果没有 Enable 任何 Measurement Module，`Measure` 仍会写系统温场快照，并产生一条
“没有模块”的 Warning。这是正常的练习结果。若已 Enable 四通道教学模块，每个场点会按
四个逻辑行键写四行模块数据。只有模块实际返回原始序列时，运行目录才会另外出现
`rawdata/`。

`Measure` 执行时会先取得一次温场快照，再调用本次 Run 已冻结的 Enabled 模块。一次调用
写一行还是多行，由这些模块声明的逻辑行键决定；初次操作不需要手工设置这个编号。

## Pause、Stop 与模块收尾

- Pause 冻结框架等待和 `ModuleAPI.sleep()` 计时，不主动关闭模块输出。
- Stop 在安全检查点取消测量，但不向 System Instrument 发送 Set 或 Hold。
- 正常完成、Stop 和 Error 都会向模块发送 `run_end`。真实模块默认在这里关闭本次 Run
  的输出；明确选择连续偏置时，模块可读回确认后保持。它仍保持 Enabled，只有 Disable
  或应用退出才调用 `close` 关闭输出并释放连接。
- 厂商驱动中已经阻塞的 I/O 只能等待它自己的有限 timeout，所以真实仪表调用不能无限等。

只想使用软件，请继续阅读 [操作手册](../OPERATIONS.md) 和
[Data Browser](../guides/data-browser.md)。准备编写 Measurement Module 或 System Instrument 时，再进入
[第一个测量模块](../development/first-module.md)。
