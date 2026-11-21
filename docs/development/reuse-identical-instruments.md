# 多台相同仪表复用一个模块

如果实验室同时接了两台同型号的测量仪表（例如两台 Keithley 2400，一台加栅压、一台测样品），不需要改动模块源码，最简单可靠的做法是：**把模块文件夹复制一份，改一个目录名和显示名称，再各自选不同的仪表资源**。

因为主程序直接用目录名作为模块 ID，两份副本会自动拥有独立的 Enable 开关、设置窗口、后台子进程和 DAT 列。

## 只需要改哪四处？

| 位置 | 要不要改 | 怎么改 |
| --- | --- | --- |
| Instrument Scanner | **要** | 把两台物理仪表分别登记为不同的 Measurement 资源 ID |
| 模块文件夹名 | **要** | 两份副本用不同目录名（即不同的模块 ID） |
| `module.toml` 的 `name` | **要** | 改成方便区分的名称，如 `Keithley 2400 - Gate` / `Sample` |
| 模块 Settings 页 | **要** | 两份副本各自选择对应的资源 ID |
| `version`、`backend.py`、`frontend.py`、核心配置、SEQ 的 `Measure` | **不要** | 保持完全一致即可 |

## 操作步骤（以两台 Keithley 2400 为例）

### 1. 在扫描器中分别登记两台仪表

打开 [Instrument Scanner](../guides/instrument-scanner.md) 扫描 VISA。新发现且未分配给系统仪表的地址默认勾选 **Keep for Measurement Module**，分别改成清晰稳定的 **Resource ID** 并保存：

| Resource ID | VISA 地址 |
| --- | --- |
| `k2400_gate` | `GPIB0::24::INSTR` |
| `k2400_sample` | `GPIB0::25::INSTR` |

### 2. 复制并重命名模块目录

把 `modules/keithley_2400/` 复制成两份（目录名必须小写字母开头，只含小写字母、数字和下划线；不要在 `module.toml` 里手动添加 `id` 字段）：

```text
modules/
├─ keithley_2400_gate/
└─ keithley_2400_sample/
```

原 `keithley_2400/` 目录如仍保留在 `modules/` 中，平时保持 Disabled 即可。

### 3. 修改各自 `module.toml` 的显示名

保留原有的 `version` 不变，只修改 `name`：

```toml
# modules/keithley_2400_gate/module.toml
name = "Keithley 2400 - Gate"
```

```toml
# modules/keithley_2400_sample/module.toml
name = "Keithley 2400 - Sample"
```

> 以后升级该模块代码时，记得把新版代码同步复制到两份目录中。

### 4. 分别启用并绑定不同资源

重启软件（或刷新模块列表）后：

1. 在 **Modules** 中勾选 **Keithley 2400 - Gate**，在 Settings 中选择 `k2400_gate`，点击 **Apply Settings**。
2. 勾选 **Keithley 2400 - Sample**，在 Settings 中选择 `k2400_sample`，点击 **Apply Settings**。
3. 重新保存一次 `.seq`，同名 `.modules.toml` 会按两个新模块 ID 分别保存各自的设置。

!!! warning "两份副本必须绑定不同的物理资源"

    点击 **Apply Settings** 时，主框架会按解析后的实际 VISA 地址检查该物理仪表是否已被另一个已启用模块占用，
    防止两份副本同时打开同一台硬件。如果一个模块包含多台仪器（如源表 + 纳伏表 + 切换器），所有涉及的物理资源也都必须彼此错开。

两台仪表挂在同一个 GPIB 控制器上、但 GPIB 地址不同是可以同时工作的；不过共享总线时并行测量可能略慢，首次接真机建议先观察是否需要适当放宽超时。

## 运行与数据文件的变化

- **`T Measure` 不用改**：两份已启用的副本会自动在同一轮 `Measure` 中并行测量。
- **自定义 SEQ 指令自动区分**：右侧指令栏会按两份 `name` 分别列出各自命令组，不会混淆。
- **DAT 列名自动消歧**：
  - 在默认的紧凑 DAT 模式下，若两份副本声明了相同的列名前缀（例如 `Delta_R1(ohm)`），核心会自动在前缀后加字母区分（如 `DeltaA_R1(ohm)` 与 `DeltaB_R1(ohm)`）；
  - 在兼容宽表模式下，列名自动带模块目录 ID 前缀（如 `keithley_2400_gate.Resistance(Ohm)`）。
- 若模块输出 `rawdata`，每份副本各自写独立的 rawdata 文件。

## 首次上机检查顺序

1. 先单独 Enable 第一份，在低风险安全输出下跑一次 `Measure` 确认正常，再 Disable；
2. 再单独 Enable 第二份做同样检查；
3. 两份同时 Enable 跑一次安全测量，打开 DAT 核对两组列都有值且没有串列；
4. 确认无误后再逐步施加实验输出。
