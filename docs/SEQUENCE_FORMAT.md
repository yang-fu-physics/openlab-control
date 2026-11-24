# SEQ 格式参考

`.seq` 是纯文本格式的实验序列文件，既可以在主窗口里通过双击图形化编辑，也可以用文本编辑器直接查看或审查。

## 基本语法

每条指令占一行，行首用 `T` / `F` 标记启用或禁用：

```text
<T|F> <command>
```

- `T`：启用该行；
- `F`：禁用该行（如果是 `Scan`，整个扫描子树都会跳过，但子行自身的 `T/F` 状态保留）；
- 缩进只为了方便人眼阅读，真正的嵌套层级由 `Scan ...` 和对应的 `End Scan` 决定；
- 文件以 `End Sequence` 结束；
- 遇到未知厂商行会保留并提示 Warning；结构错误或带参数的旧格式 `Measure` 会报 Error 并阻止运行。

多层嵌套示例：

```text
T Scan Temperature 300.000 K to 10.000 K in 10 steps at 5.000 K/min, Settle
T     Scan Field -10000.00 Oe to 10000.00 Oe in 21 steps at 5000.00 Oe/min, Settle
T         Measure
T     End Scan
T End Scan
T End Sequence
```

## 配套模块设置文件（`.modules.toml`）

在界面保存 `experiment.seq` 时，程序会在同目录自动保存 `experiment.modules.toml`，记录本次实验关联的模块 Settings 值：

- 重新打开 `experiment.seq` 时自动把参数填进模块界面（打开历史运行目录中的 `sequence.seq` 时也会兼容读取其 `module_settings/*.settings.toml`）；
- **只填界面，不自动 Enable，也不自动 Apply**；
- `Call Sequence` 调用的子序列不会在运行中途悄悄切换模块设置；
- 缺失对应模块、版本变更或伴随文件损坏时不会丢失 `.seq` 本体，只给出明确提示。

---

## 全部指令速查

### Set Datafile（指定输出数据文件）

```text
T Set Datafile open|create experiment.dat
T Set Datafile create external C:\Experiment Data\sample.dat
```

- **模式**：`create`（新建/覆盖）、`open`（向已有文件追加）、`open|create`（存在则追加、否则新建）；
- **`external`**：明确授权把实验 DAT 写到运行目录之外的自定义路径（其他快照和 `instrument_status.dat` 仍留在自动运行目录）；
- 在界面编辑弹窗中点击 `Browse…` 可直接用系统文件对话框选择路径。

### Wait（等待）

```text
T Wait For 10.0 secs
T Wait For Temperature
T Wait For Field
T Wait For Temperature, Field
T Wait For Temperature, Field, Delay 60.0 secs
```

- 可勾选等待主温度（`sample_temp` 角色）和/或主磁场（`field` 角色）稳定，并可附加延时 `Delay`（有效范围 `0`–`31,536,000` 秒，1 位小数）；
- 同时勾选多项条件时，会等待所有勾选控制器**同时**进入核心 `Stable` 状态，然后再开始执行 `Delay` 延时；
- `Pause` 会冻结稳定超时与延时计时，`Stop` / `Error` 可立即打断。

### Set Temperature / Set Field（单点控温 / 控场）

```text
T Set Temperature 20.000 K at 5.000 K/min
T Set Field 10000.00 Oe at 5000.00 Oe/min
T Set Field 1.000000 T at 0.500000 T/min
```

- 温度固定使用角色为 `sample_temp` 的控制器面板，磁场固定使用角色为 `field` 的控制器面板；
- 单点 `Set` 指令均为非阻塞下发：校验并写入目标与速率后立即进入下一条指令，便于同时启动变温与变场；如需等待稳定，请在后续放置 `Wait` 指令；
- 磁场支持 `Oe`（2 位小数）或 `T`（6 位小数），同一条指令内目标与速率须用相同单位。

### Scan Temperature（线性 / 对数 / 列表扫温）

```text
# 线性扫温（steps 为包含起点与终点的总点数，范围 1–100,000）
T Scan Temperature 300.000 K to 10.000 K in 10 steps at 5.000 K/min, Settle
T     Measure
T End Scan

# 对数扫温（起止温度须为正值，中间点按控制面板目标精度量化）
T Scan Temperature Log 300.000 K to 2.000 K in 21 steps at 5.000 K/min, Settle
T     Measure
T End Scan

# 列表扫温（保留原顺序和重复点，不排序不去重，最多 100,000 点）
T Scan Temperature List [300, 100, 20, 20, 4.2] K at 5.000 K/min, Settle
T     Measure
T End Scan
```

- `Settle`：每点等待核心判定完全稳定后再执行子指令；
- `Sweep`：先 Set 起点并等待稳定，执行一轮子指令采集初始点；再一次性 Set 终点并连续扫过。中间不重新设点、不逐点判稳，实际读数越过下一阈值即执行一轮子指令，按原顺序追赶，不跳过已越过的中间阈值。若等待阈值期间实际读数进入终点容差或越过终点，则执行最后一次终点测量后结束本次 Scan，不在终点补做跳过的中间轮次；若在某一轮子指令执行期间已到达终点，则完成当前整组子指令后直接结束 Scan 并继续后续 SEQ；
- `Sweep` 的温度 List 必须沿同一方向排列，可以保留重复的中间阈值；折返列表会在启动移动前拒绝。单点 Sweep 只 Set、判稳并执行一轮子指令；
- 连续 Sweep 的子指令不应重新设置同一温度／磁场，也不应等待该参数稳定；测量和延时期间仍继续扫。点标签是判断阈值，数据文件中的温度／磁场来自实际读数；
- 弹窗支持直接输入步长（`Log` 模式下为 `Points / decade`），失焦或确认时自动取整同步 `Points` 并回写实际步长；
- 执行前会整表校验所有点是否在温度上下限内，任意一点越界都会在启动移动前报错。

### Scan Field（扫场）

```text
T Scan Field -90000.00 Oe to 90000.00 Oe in 181 steps at 5000.00 Oe/min, Settle
T     Measure
T End Scan

# 可选“就近极性”：根据执行时当前真实磁场决定按原路径或整条反号路径扫描
T Scan Field 9.000000 T to 3.000000 T in 7 steps at 1.000000 T/min, Settle, Nearest +/- Polarity
T     Measure
T End Scan
```

开启 `Nearest +/- Polarity` 后，运行到该步骤时会读回当前新鲜磁场值，比较离起点还是离起点负值更近（例如当前 `-6 T` 时，自动执行 `-9 T → -3 T`），所有点同样在移动前通过上下限校验。

### Scan Time（定时扫描）

```text
T Scan Time 60.0 secs in 61 steps
T     Measure
T End Scan
```

使用单调时钟调度（不受系统校时影响）：第 1 点在 `t = 0`，最后一点在 `duration` 处；范围 `0`–`31,536,000` 秒，`steps` 为 `1`–`1,000,000`。

### System Instrument 无参数指令

配置了无参数指令的系统仪表（例如压缩机开关）会直接按显示名出现在 SEQ 中：

```text
T Compressor On
T Compressor Off
```

这类指令不产生 DAT 行；对应系统仪表未配置时，加载会保留原文并提示 Warning，运行前拦截。

### Measurement Module 自定义指令与扫描

```text
T Module Command "oe1022d_lockin" "auto_range" {"auto_reserve":true}
T Module Scan "keithley_6221_ac" "scan_current" {"point_mode":"Linear","start":"100u","stop":"500u","steps":5,"settle_seconds":3.0}
T     Measure
T End Scan
T Module Scan "keithley_6221_ac" "scan_current" {"point_mode":"Log","start":"10u","stop":"1m","steps":11,"settle_seconds":3.0}
T     Measure
T End Scan
T Module Scan "keithley_6221_ac" "scan_current" {"point_mode":"List","points":["100u","200u","500u"],"settle_seconds":3.0}
T     Measure
T End Scan
```

- 依次为：模块目录 ID、稳定指令 ID、标准 JSON 参数对象（禁止 `NaN`/`Infinity`）；
- 声明了 `scan_modes` 的扫描指令须显式携带 `point_mode`（`Linear`、`Log` 或 `List`）；纯列表扫描指令（如 `scan_frequency`）仅携带列表字段；
- 声明了 `point_limits` 的指令会在参数窗口自动同步模块 Settings 上限（如 `max_current`、`max_voltage`）；
- 本身不写 DAT 行，需要在循环内显式放 `Measure`；
- 普通命令 Warning 继续下一条，扫描点 Warning 跳过该点子树继续下一点，Error 中止整个 SEQ；
- 对应模块未安装或未启用时，行在编辑器里标红并保留原文本，运行预检阻止执行。

### Measure（采集数据）

唯一合法格式（无任何参数）：

```text
T Measure
```

参与测量的模块由 Run 前在 **Modules** 中勾选的状态决定；重复采集请用 `Scan Time` 或多次插入 `Measure`。每个逻辑通道槽位并行调用参与该槽位的模块，写出一行 DAT。

### Remark / Call Sequence / 仿真故障注入

```text
T Remark Cooldown complete; begin transport scan
T Call Sequence subsequences\field_loop.seq
T Inject Warning SIM_WARNING simulated warning
T Inject Error SIM_ERROR simulated fatal error
```

- `Remark` 记入事件日志，不动硬件；
- `Call Sequence` 按调用者目录解析相对路径，预检阶段检查递归循环与语法错误；
- `Inject Warning / Error` 仅用于仿真测试事件与停机流程。
