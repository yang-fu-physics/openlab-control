# SEQ 格式参考

## 基本规则

- 文件扩展名为 `.seq`。
- 每条指令单独一行。
- 启用指令行以 `T ` 开头；禁用指令行以 `F ` 开头。
- Scan 子指令缩进四个空格。
- 每个 Scan 以同级 `End Scan` 结束。
- 文件以 `End Sequence` 结束。
- 内存中使用树结构，因此嵌套层数没有人为限制。
- 保存编码为 UTF-8；加载还兼容 UTF-8 BOM、UTF-16 和 GB18030。

示例：

```text
T Scan Temperature 10.000 K to 30.000 K in 3 steps at 5.000 K/min, Settle
T     Scan Field -1.000000 T to 1.000000 T in 5 steps at 0.500000 T/min, Settle
T         Measure devices=transport
T     End Scan
T End Scan
T End Sequence
```

禁用示例：

```text
F Set Temperature 10.000 K at 2.000 K/min in Settle mode
F Scan Field -0.100000 T to 0.100000 T in 3 steps at 0.050000 T/min, Settle
T     Measure devices=transport
T End Scan
T End Sequence
```

第二个例子中 Scan 行为 `F`，因此整个 Scan 及其 Measure 子命令都不执行。Measure 自身仍保留 `T`；以后重新启用 Scan 时，原有子命令状态会恢复生效。

## 指令

### Initialize

```text
T Initialize <model> model <config-path>
```

示例模板：

```text
T Initialize Lakeshore372AC model \configs\lakeshore372ac\20260625161624.ini
```

仿真版只记录该指令。真实插件可在未来通过初始化命令路由加载设备特定配置。

### Set Datafile

```text
T Set Datafile <mode> <path>
```

模式：

- `create`：新建或覆盖。
- `open`：只打开已有文件，不存在则 Error。
- `open|create`：存在则追加，不存在则创建。

默认安全配置会把外部绝对路径重定向到本次运行目录。

### Wait

```text
T Wait For 10.0 secs
```

等待可被 Pause 和 Stop 中断。

### Set Temperature

```text
T Set Temperature 10.000 K at 5.000 K/min in Settle mode
```

Settle 等待中央数值判稳。名称中包含 `settle` 的模式，例如 `Fast Settle`，也按等待稳定处理。Sweep 只发出目标，不等待持续稳定时间。

### Set Field

```text
T Set Field 1.000000 T at 0.500000 T/min in Settle mode
T Set Field 10000.000000 Oe at 5000.000000 Oe/min in Sweep mode
```

框架会转换 T/Oe 到设备配置单位，然后执行安全检查。

### Scan Temperature

```text
T Scan Temperature 10.000 K to 30.000 K in 3 steps at 5.000 K/min, Settle
T     <child commands>
T End Scan
```

点数包含起点和终点。点数为 1 时只使用起点。

### Scan Field

```text
T Scan Field -1.000000 T to 1.000000 T in 5 steps at 0.500000 T/min, Settle
T     <child commands>
T End Scan
```

支持 T 和 Oe。

### Scan Time

```text
T Scan Time 60.0 secs in 60 steps
T     Measure
T End Scan
```

点数包含 `0 s` 和总时间。如果只有一个点，在 `0 s` 执行一次子指令。

### Measure

最简格式：

```text
T Measure
```

测量全部启用插件。扩展参数：

```text
T Measure devices=transport,bridge repeats=3 interval=1s
```

- `devices`：逗号分隔的设备 ID，或 `all`。
- `repeats`：重复次数。
- `interval`：重复之间的秒数。

### Remark

```text
T Remark Cooling branch
```

写入事件日志，不改变设备状态。

### Call Sequence

```text
T Call Sequence examples/subsequence.seq
```

相对路径首先相对于调用它的 SEQ 所在目录解析。执行器检测递归循环；文件缺失、解析错误或循环调用均为 Error。

### 仿真故障指令

```text
T Inject Warning SIM_WARNING Example warning
T Inject Error SIM_ERROR Example fatal error
```

仅用于框架测试，不应出现在真实实验序列中。

## 未知指令

解析器不会删除未知指令：

- 编辑器以黄色显示。
- 保存时保留原文字。
- 运行时产生一次 Warning 并跳过。

当新增命令插件或解析支持后，旧文件仍可再次打开。

## 插入规则

- 选中普通指令：新命令插入到它之后并保持同级。
- 选中 Scan 开始行：新命令追加为其子指令。
- 选中 End Scan：新命令追加为该 Scan 的子指令。
- 选中 End Sequence：新命令追加到序列末尾。

## 编辑、启用与禁用

选择规则：

- 单击选择一行。
- `Ctrl+Click` 增加或移除单行选择；`Shift+Click` 选择连续范围。
- 右键已选中的一行会保留整组选择；右键未选中的行会先切换为只选择该行。
- Scan 开始行与对应 `End Scan` 指向同一个命令节点；两者同时选中时只处理一次。

SEQ 窗口右键菜单：

| 操作 | 快捷键 | 语义 |
|---|---|---|
| `Disable` | `Ctrl+D` | 把全部选中命令标为禁用，保存时行首写为 `F` |
| `Enable` | `Ctrl+E` | 把全部选中命令恢复启用，保存时行首写为 `T` |
| `Delete` | `Delete` | 删除全部选中命令节点；Scan 会连同全部子命令删除 |
| `Copy` | `Ctrl+C` | 按文档顺序复制全部选中命令；Scan 会递归复制全部嵌套层级 |
| `Paste` | `Ctrl+V` | 在当前焦点行按插入规则依次插入副本，内部节点 ID 全部重新生成 |

结构性批量操作遵循“最外层节点优先”：若一个 Scan 和它的任意后代同时被选择，Copy/Delete 只处理该 Scan 一次，因为副本或删除范围已经包含后代。多行 Copy 不依赖点击顺序，始终按 SEQ 中从上到下的顺序进入剪贴板；Paste 保持该顺序，并选中全部新建的顶层副本。

右键 Scan 的 `End Scan` 行时，上述操作作用于对应 Scan 块。`End Sequence` 不是命令，只允许把已复制命令粘贴到序列末尾。Disable/Enable 直接修改每个被选命令自身的 `T/F`，因此父 Scan 和子项都被选择时，两者自身标志都会改变。

禁用规则：

- 普通 `F` 指令在运行时完全跳过，不执行设备操作或测量。
- `F` Scan 的整个子树跳过；子命令自身的 `T/F` 标记不被递归改写。
- 禁用命令显示为灰色删除线；因父 Scan 禁用而暂时不活动的子命令也显示为灰色。
- 执行日志产生 `STEP_SKIPPED_DISABLED` Info，便于追溯为什么某一步没有执行。
- `End Scan` 和 `End Sequence` 是结构行，规范保存时始终使用 `T`。
- 运行期间 Disable、Enable、Delete 和 Paste 被锁定；Copy 仍可使用。

## 兼容性说明

用户提供的模板：

```text
T Initialize Lakeshore372AC model \configs\lakeshore372ac\20260625161624.ini
T Set Datafile open|create C:\Users\liuju\Desktop\data\20260625-k12601.dat
T Scan Time 60.0 secs in 60 steps
T     Measure
T End Scan
T End Sequence
```

在本版本中能够无警告解析，并在未编辑的情况下逐行原样保存。尚未收到的厂商命令格式需通过额外样例补充解析测试。
