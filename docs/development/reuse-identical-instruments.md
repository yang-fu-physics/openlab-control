# 多台相同仪表复用一个模块

如果实验室同时接了两台同型号的测量仪表（例如两台 Keithley 2400，一台加栅压、一台测样品；或两台 OE1022D 锁相仪），**不需要复制模块文件夹，也不需要改动模块源码**——直接在 **Modules** 管理器里使用 **Add Instance…** 创建独立实例即可。

所有实例共享 `modules/<模板目录>/` 下的同一份源码（`git pull` 或替换新版目录后所有实例自动同步升级），但各自拥有独立的 Enable 开关、独立浮动窗口、独立后台子进程、独立 `module_data/<实例ID>/settings.toml` 设置文件、独立 SEQ 指令组和独立 DAT 列。

## 操作步骤（以两台 Keithley 2400 为例）

### 1. 在 Instrument Scanner 中分别登记两台仪表

打开 [Instrument Scanner](../guides/instrument-scanner.md) 扫描 VISA。把两台物理仪表都勾选 **Keep for Measurement Module**，分别起清晰稳定的 **Resource ID** 并保存：

| Resource ID | VISA 地址 |
| --- | --- |
| `k2400_gate` | `GPIB0::24::INSTR` |
| `k2400_sample` | `GPIB0::25::INSTR` |

### 2. 在 Modules 管理器里添加实例

打开主窗口工具栏 **Modules**，点击底部 **Add Instance…**（**无需先 Disable 其他已启用的模块**，只要当前没有运行 SEQ 即可）：

1. **Source module**：选择基础模块（如 `Keithley 2400 (keithley_2400)`）；
2. **Instance ID**：输入唯一实例 ID（小写字母开头，只含小写字母、数字和下划线，例如 `keithley_2400_gate`；默认自动建议 `keithley_2400_2`）；
3. **Display name**：输入方便区分的显示名称（例如 `Keithley 2400 - Gate`）。

确认后，实例会立即出现在 Modules 列表中，并持久化保存到 `configs/module_instances.toml`：

```toml
[[instances]]
id = "keithley_2400_gate"
module = "keithley_2400"
name = "Keithley 2400 - Gate"

[[instances]]
id = "keithley_2400_sample"
module = "keithley_2400"
name = "Keithley 2400 - Sample"
```

如需删除不再使用的额外实例：先取消勾选使其处于 Disabled 状态，选中该行后点击 **Remove Instance** 即可（不会删除 `modules/` 下的源码或历史数据；基础目录项不可删除）。

### 3. 分别启用并绑定不同资源

1. 勾选 **Keithley 2400 - Gate**，在其窗口的 Settings 中选择 `k2400_gate`，点击 **Apply Settings**。
2. 勾选 **Keithley 2400 - Sample**，在其窗口的 Settings 中选择 `k2400_sample`，点击 **Apply Settings**。
3. 两个窗口可以并排对比，也可以关闭隐藏（实时数据继续显示在主窗口左侧监视卡）。
4. 重新保存一次 `.seq`，同名 `.modules.toml` 会按各自的实例 ID 分别保存参数。

!!! warning "每个实例必须绑定不同的物理资源"

    点击 **Apply Settings** 时，主框架会按解析后的实际 VISA 地址检查该物理仪表是否已被另一个已启用模块占用，
    防止两个实例同时打开同一台硬件。如果一个模块包含多台仪器（如源表 + 纳伏表 + 切换器），所有涉及的物理资源也都必须彼此错开。

两台仪表挂在同一个 GPIB 控制器上、但 GPIB 地址不同是可以同时工作的；不过共享总线时并行测量可能略慢，首次接真机建议先观察是否需要适当放宽超时。

## 运行与数据文件的变化

- **`T Measure` 不用改**：所有已启用实例会在同一轮 `Measure` 中由各自独立子进程并行测量。
- **自定义 SEQ 指令与限幅自动区分**：右侧指令栏会按各自的 `Display name` 分组列出命令（如各自的 `Scan Current` / `Scan Voltage`），参数窗口的限幅同步（如 `max_current` / `max_voltage`）也各自绑定对应实例的 Settings。
- **DAT 列名自动消歧**：
  - 在默认的紧凑 DAT 模式下，若多个实例声明了相同列名，核心会自动加字母区分（例如 `DeltaA_R1(ohm)` / `DeltaB_R1(ohm)` 或 `X1pA(V)` / `X1pB(V)`）；
  - 在兼容宽表模式下，列名自动带实例 ID 前缀（如 `keithley_2400_gate.Resistance(Ohm)`）。
- 每次 Run 的配置快照会自动包含 `configs/module_instances.toml`；若模块输出 `rawdata`，每个实例各自写独立的 rawdata 文件。

## 首次上机检查顺序

1. 先单独 Enable 第一个实例，在低风险安全输出下跑一次 `Measure` 确认正常，再 Disable；
2. 再单独 Enable 第二个实例做同样检查；
3. 两个实例同时 Enable 跑一次安全测量，打开 DAT 核对两组列都有值且没有串列；
4. 确认无误后再逐步施加实验输出。
