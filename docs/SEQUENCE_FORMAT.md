# SEQ 格式参考

OpenLab Control 的 `.seq` 是可直接阅读和编辑的单行指令文件。右侧 Sequence Command Bar 双击命令后弹出参数窗口并插入；SEQ 编辑器双击已有行可再次修改。

## 基本规则

每行格式：

```text
<T|F> <command>
```

- `T`：该命令启用。
- `F`：该命令禁用；若是 Scan，整个子树都跳过，但子命令自己的 T/F 状态保留。
- 每个命令占一行。
- Scan 以 `End Scan` 结束，文件以 `End Sequence` 结束。
- 缩进只用于显示；真正层级由 Scan/End Scan 决定。
- 解析器保留未知厂商行并给 Warning；结构错误、旧 Initialize、带参数 Measure 给 Error，Run 会被阻止。

## 模块设置伴随文件

SEQ 指令仍只保存在 `.seq` 文本中。File → Save 会把当前实验已关联模块及所有 Enabled
模块的 Settings 页值另存为同目录同名文件：

```text
experiment.seq
experiment.modules.toml
```

File → Open/Load `experiment.seq` 时会自动读取 `experiment.modules.toml`。导入只改变
模块 Settings 页的期望值：

- 不自动 Enable 或 Disable 任何模块；
- 不连接仪表，不调用模块 `configure`，不发送命令；
- 已 Enabled 模块会显示 `SEQ settings imported — not applied`；
- Disabled 模块以后 Enable 时才看到导入值，仍须人工核对并点击 Apply Settings；
- 缺少模块或模块版本变化只给 Warning，并保留原始设置供安装/升级后检查；
- 伴随文件损坏、超过 1 MiB、格式版本错误或含非法模块 ID 时，模块设置整体不导入，
  但 `.seq` 指令仍可打开；
- 没有伴随文件的旧 SEQ 与以前完全相同。

运行目录使用既有快照结构。打开其中的标准 `sequence.seq` 时，如果没有同名伴随文件，
程序会兼容读取同目录 `module_settings/*.settings.toml`。`Call Sequence` 不会在运行中
切换被调用文件的伴随设置，防止一次 Run 中隐式重配真实仪表；只有用户在界面中 Load
的顶层 SEQ 才导入设置。

多层嵌套示例：

```text
T Scan Temperature 300.000 K to 10.000 K in 10 steps at 5.000 K/min, Settle
T     Scan Field -10000.00 Oe to 10000.00 Oe in 21 steps at 5000.00 Oe/min, Settle
T         Measure
T     End Scan
T End Scan
T End Sequence
```

## 指令

### Set Datafile

运行目录内文件：

```text
T Set Datafile open|create experiment.dat
```

用户明确选择的自定义目录：

```text
T Set Datafile create external C:\Experiment Data\sample.dat
```

Mode：

- `create`：覆盖/新建目标 DAT；
- `open`：目标必须已存在并追加；
- `open|create`：存在则追加，否则创建。

`external` 是该命令对自定义路径的明确授权。运行目录中的 SEQ、配置、模块设置和 Status 快照不会随 DAT 移走。

在图形界面插入或双击 `Set Datafile` 后，可点击 `Browse…`。框架直接调用 Windows
原生文件选择窗口：`open` 选择已有 DAT，`create` / `open|create` 选择或新建 DAT；
确认后写入绝对路径并自动切换为 `Custom folder`。路径仍可手动输入。

### Wait

```text
T Wait For 10.0 secs
```

Pause 会冻结等待进度；Stop/Error 可在最多约一个运行时检查周期内打断。
等待时间必须在 0 到 31536000 秒之间；手写 SEQ 与运行时使用同一限制。

### 设备选择

温度/磁场 Set 和 Scan 都可在末尾显式选择配置中的设备：

```text
T Set Temperature 20.000 K at 5.000 K/min in Settle mode using device "cryostat_primary"
T Scan Field 0.00 Oe to 1000.00 Oe in 11 steps at 500.00 Oe/min, Settle using device "magnet A"
```

参数窗口按设备类型列出允许控制的配置 ID，并随选择项切换上下限和最大速率。省略
`using device` 时选择该类型唯一的 primary；旧文件中的 `temperature`/`field` 角色名也
按此规则兼容。显式 ID 会随保存和重新打开保持，ID 不得指向错误类型、只读设备或未连接
设备。

### Set Temperature

```text
T Set Temperature 20.000 K at 5.000 K/min in Settle mode
T Set Temperature 300.000 K at 10.000 K/min in Sweep mode
```

- `Settle`：发送目标后等待中央数值判稳。
- `Sweep`：发送目标后立即进入下一条顶层指令。

Target 与 Rate 在弹窗和执行前都使用配置文件的 `min/max/max_rate` 检查。温度显示三位小数。

### Set Field

```text
T Set Field 10000.00 Oe at 5000.00 Oe/min in Settle mode
T Set Field 1.000000 T at 0.500000 T/min in Sweep mode
```

设备默认原生单位为 Oe；SEQ 可选 Oe 或 T，但 Target 与 Rate 必须同单位。中央换算到设备单位后再做限制检查。Oe 显示两位，T 显示六位。

### Scan Temperature — Linear

```text
T Scan Temperature 300.000 K to 10.000 K in 10 steps at 5.000 K/min, Settle
T     Measure
T End Scan
```

`steps` 是包含起点和终点的点数。每个点到达后执行全部子命令。
Linear Temperature/Field Scan 的 `steps` 范围为 1 到 100000，Rate 必须大于零。

- `Settle`：每点等待中央 Stable。
- `Sweep`：每点等待进入目标容差，不要求斜率/驻留判稳，再执行子命令。

### Scan Temperature — List

```text
T Scan Temperature List [300, 100, 20, 20, 4.2] K at 5.000 K/min, Settle
T     Measure
T End Scan
```

- 保留原顺序和重复点，不排序、不去重。
- 方括号用于清楚标识列表边界；每个点保留输入的小数位，不会自动补成三位小数。
- 旧版不带方括号的列表仍可读取；未编辑时保存也不会改写原始行。
- 最多 100000 点。
- Run 前整表转换并验证；任一点越过温度上下限则在移动第一点前 Error。
- 仍可嵌套 Field/Time/Temperature Scan 和 Measure。

### Scan Field

```text
T Scan Field -90000.00 Oe to 90000.00 Oe in 181 steps at 5000.00 Oe/min, Settle
T     Measure
T End Scan
```

起点、终点和速率必须使用同一单位。所有点在移动前整体验证。

### Scan Time

```text
T Scan Time 60.0 secs in 61 steps
T     Measure
T End Scan
```

第一个点位于 `t=0`，最后一个点位于指定 duration。调度使用单调时钟，避免系统日期变化影响间隔。
duration 范围为 0 到 31536000 秒，`steps` 范围为 1 到 1000000。
解析器和执行器都会验证这些边界，不会把非法值静默截断为零或一点。

### Measure

唯一有效格式：

```text
T Measure
```

Measure 无参数。旧格式例如：

```text
T Measure devices=transport repeats=3 interval=1s
```

会产生 Error 并阻止 Run，不做兼容转换。重复测量用 Scan Time 或重复插入 Measure；选择测量方案则在运行前通过 Modules Manager Enable/Disable。

执行语义：

1. 锁定本次 Run 开始时所有 Enabled 模块及其设置。
2. 核心按所有可选 `slots` 的并集展开；每个逻辑槽位调用参与模块，每次
   `measure(slot, api)` 返回一行结果。
3. 同一槽位的参与模块并行，结果合入该通道对应的一行；CH1–CH4 仍是四行，未参与模块列为空，
   没有槽位钩子的模块每行重新测量。
4. 等所有槽位结束后才继续下一条 SEQ。
5. 没有 Enabled 模块时弹一个锁存 Warning，写一行系统状态，继续 SEQ。
6. 模块 Warning 继续；模块 Error 使运行进入 Faulted，并以 `reason="error"` 发送
   `run_end`；`close` 只在 Disable 或应用退出执行。

### Remark

```text
T Remark Cooldown complete; begin transport scan
```

写入事件日志，不改变设备。

### Call Sequence

```text
T Call Sequence subsequences\field_loop.seq
```

相对路径以调用者 SEQ 所在目录解析。循环调用、文件不存在或子 SEQ 解析 Error 都会使主运行 Faulted。子 SEQ 中的 Measure 使用主运行锁定的同一模块集合和 DAT Schema。

### 仿真故障指令

```text
T Inject Warning SIM_WARNING simulated warning
T Inject Error SIM_ERROR simulated fatal error
```

仅用于验证事件与 Stop 路径；真实实验 SEQ 通常不需要。

## 已删除的 Initialize

0.10.0 不再接受：

```text
T Initialize Lakeshore372AC model ...
```

模块初始化由 Modules Manager 的 Enable 自动调用。仓库保留 `examples/template_original.seq` 作为用户提供原文件和解析回归材料，但它是明确的旧格式示例，不能直接 Run。新示例是 `examples/module_measurement.seq`。

## 编辑操作

SEQ 支持单行或多行选择：

- 右键：Disable、Enable、Delete、Copy、Paste；
- 键盘：Delete、Ctrl+C、Ctrl+V，以及相应启用/禁用快捷键；
- 选择父 Scan 与其子行时，结构操作只作用于最外层选中节点，避免重复删除/复制；
- 复制完整 Scan 会包含其全部子树；
- SEQ Running/Paused/Stopping 时所有修改操作禁用，Copy 仍可使用。

Disable 的序列化示例：

```text
F Scan Field -1000.00 Oe to 1000.00 Oe in 3 steps at 500.00 Oe/min, Settle
T     Measure
T End Scan
```

此时 Scan 及 Measure 都不执行；重新 Enable Scan 后，Measure 原有 `T` 状态恢复生效。
