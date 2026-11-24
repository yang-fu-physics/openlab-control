# 运行第一条 SEQ

这一节带你用内置仿真跑通一条完整的“定温 → 扫场 → 测量”自动序列，全程不需要真实仪表。

**什么是 SEQ？** 它是 OpenLab Control 的实验脚本文件（`.seq`），结构类似 MultiVu：每行一条指令，扫描指令（`Scan ...`）和对应的 `End Scan` 构成一层缩进，可以任意多层嵌套。

## 1. 先打开三个内置仿真面板

全新安装默认关闭所有仿真。请先关闭主程序，打开 **Instrument Scanner**：

1. 一直切到最后一页 **Review & Save**；
2. 勾选 **Simulated Temperature**、**Simulated Magnetic Field** 和 **Simulated 2nd Stage**；
3. 确认面板顺序无误后点击保存；
4. 重新启动 OpenLab Control，确认底部三个面板都出现了仿真读数。

（可选）点击顶部工具栏 **Modules**，勾选自带的 **`Simulated Transport`**，这样待会儿就能看到真实的四通道模拟电阻数据写入文件。

## 2. 编排一条扫描序列

1. 点击 **File → New**，新建的序列会自动带有一行末尾的 `End Sequence`。
2. 选中 `End Sequence`，在右侧 **Sequence Command Bar** 中依次双击以下指令并填好参数：
   - **Remark**：填写备注 `First simulated run`
   - **Set Datafile**：Mode 选 `create`，Location 选 `Run folder`，文件名填 `experiment.dat`
   - **Set Temperature**：`300 K`，速率 `10 K/min`
   - **Wait**：勾选 `Wait for Temperature`，`Delay (s)` 设为 `0.0`
   - **Scan Field**：从 `0 Oe` 到 `200 Oe`，共 `3` 个点，速率 `5000 Oe/min`，模式 `Settle`
3. 选中刚插入的 **Scan Field**（或它的 **End Scan**），双击右侧的 **Measure**，它会自动缩进放进 `Scan Field` 内部。
4. 点击 **File → Save As**，保存为 `first-run.seq`。

完成后，序列编辑器里会显示类似下面的内容（缩进和 `End Scan` 由程序自动维护）：

```text
T Remark First simulated run
T Set Datafile create experiment.dat
T Set Temperature 300 K at 10 K/min
T Wait For Temperature
T Scan Field 0 Oe to 200 Oe in 3 steps at 5000 Oe/min, Settle
T     Measure
T End Scan
T End Sequence
```

> **几个小符号的含义**：
> - 行首的 `T` 表示启用该行，改为 `F`（右键 Disable）则跳过执行；
> - `Set Temperature` / `Set Field` 下发目标后立即继续，配合 `Wait` 等待温度/磁场稳定和延时；`Scan` 的 `Settle` 模式每点等待稳定后再测，`Sweep` 模式到达目标容差即测；
> - `Set Datafile` 的三种模式：`create`（新建/覆盖）、`open`（向已有文件追加）、`open|create`（存在则追加、不存在则新建）。

## 3. 点击运行并查看产出文件

1. 点击左侧控制区的 **Run** 按钮；
2. 观察左侧进度条和模块监视卡，直到状态变为 **Completed**；
3. 打开项目目录下的 `runs/<时间戳>_first-run/`，每次实验都会自动保存一整套文件：

```text
runs/<时间戳>_first-run/
├─ experiment.dat            # 实验测量数据（可直接拖进 Data Browser 画图）
├─ instrument_status.dat     # 运行期间每秒记录的完整温场/状态日志
├─ events.dat                # Warning / Error / Remark 事件日志
├─ sequence.seq              # 本次实际运行的 SEQ 快照
├─ configuration/            # 当次使用的系统配置快照
└─ module_settings/          # 已启用模块的参数与初始状态快照
```

- 如果你启用了 `Simulated Transport`，`experiment.dat` 里每个磁场点会按 4 个通道写出 4 行数据（如果模块返回原始波形，还会多出一个 `rawdata/` 目录）。
- 如果没有启用任何测量模块，`Measure` 仍会记录系统温场并弹出一条“无模块”的 Warning，实验继续跑完——这是正常的练习行为。

## 4. 运行中途的 Pause 和 Stop 会做什么？

了解这两个按钮的行为对后续做真实实验非常重要：

- **Pause（暂停）**：在下一个安全检查点暂停序列推进与等待计时；**不会**断开仪表，也**不会**主动关掉模块输出。点击 Resume 后从原位置继续。
- **Stop（停止）**：取消后续序列步骤；**不会向温控仪或磁体发送新的 Set 或 Hold**（系统仪表维持自己当前目标继续运行），同时向所有测量模块发出 `run_end` 通知，模块默认在这里安全关闭自身输出，但仍保持 Enabled 状态，方便你改完参数后再次运行。

## 下一步

- 想全面掌握日常操作、看图和排错：阅读 [操作手册](../OPERATIONS.md) 和 [Data Browser](../guides/data-browser.md)。
- 想开始写自己的测量模块：进入 [写第一个测量模块](../development/first-module.md)。
