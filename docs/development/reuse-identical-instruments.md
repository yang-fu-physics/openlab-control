# 多台相同测量仪表复用一个模块

当前版本不需要新增“模块实例”功能。最简单的做法是：每台物理仪表使用一份模块目录，
这些目录中的测量代码可以完全相同。主程序把目录名当作模块 ID，因此每份副本都会有独立的
Enable、设置窗口、工作进程和 DAT 列。

下面以两台 Keithley 2400 为例。一台测栅极，另一台测样品。

## 到底需要改什么

| 位置 | 是否要改 | 修改内容 |
| --- | --- | --- |
| 仪表扫描器 | 要 | 每台物理仪表登记为不同的 Measurement 资源 |
| 模块目录名 | 要 | 每份副本使用不同目录名，也就是不同模块 ID |
| `module.toml` 的 `name` | 要 | 写成操作者能区分的名称 |
| 模块 Settings | 要 | 每份副本选择不同的 Measurement 资源 |
| `module.toml` 的 `version` | 不要 | 相同代码保留相同版本 |
| `backend.py`、`frontend.py`、仪表指令文件 | 不要 | 直接使用相同代码 |
| OpenLab Control 核心代码和主配置 | 不要 | 当前框架已能同时运行这些模块副本 |
| 普通 `Measure` | 不要 | 所有 Enabled 副本会自动参与测量 |

## 开始前先登记两台仪表

用[仪表扫描工具](../guides/instrument-scanner.md)登记两台仪表。新扫描到且没有匹配
System Instrument 的地址默认是 **Ignore**；分别把 **Use** 改为 **Measurement**，再填写
不同的资源 ID 并保存。这里登记的是物理地址，不会把仪表绑定到某个模块。

两条资源必须有不同的 ID 和不同的物理地址，例如：

| 资源 ID | VISA 地址 | 用途 |
| --- | --- | --- |
| `k2400_gate` | `GPIB0::24::INSTR` | Measurement |
| `k2400_sample` | `GPIB0::25::INSTR` | Measurement |

扫描器会保存实际的 `*IDN?` 返回值。这里不用手写或复制 `identity`。

## 复制模块目录

假设原模块目录是：

```text
modules/keithley_2400/
```

复制成两份：

```text
modules/
├─ keithley_2400_gate/
└─ keithley_2400_sample/
```

也可以在资源管理器中完成复制和重命名。目录名必须从小写字母开始，并且只包含小写字母、
数字和下划线；例如 `keithley_2400_gate`。不要在 `module.toml` 中增加 `id`，因为
`module.toml` 不接受这个字段。

原来的 `keithley_2400` 目录如果仍在 `modules/` 中，会作为第三个模块显示。暂时不使用时让它
保持 Disabled 即可。

## 每份副本只改名称

在两份副本的 `module.toml` 中分别只修改 `name`，让操作者能够区分窗口和 SEQ 指令。
保留文件中原来的 `version`，不要照下面的例子替换版本号。

第一份：

```toml
name = "Keithley 2400 - Gate"
```

第二份：

```toml
name = "Keithley 2400 - Sample"
```

`backend.py`、`frontend.py` 和仪表指令文件不需要修改。以后更新模块代码时，应把相同修改
同步到两份目录；当前方案不会自动同步副本。

## 分别选择资源

重启 OpenLab Control，或在 Modules 窗口中刷新模块列表，然后依次操作：

1. Enable **Keithley 2400 - Gate**，按提示确认这份模块代码。
2. 在它的 Settings 中选择 `k2400_gate`，再点击 **Apply Settings**。
3. Enable **Keithley 2400 - Sample**，按提示确认这份模块代码。
4. 在它的 Settings 中选择 `k2400_sample`，再点击 **Apply Settings**。

每份副本的设置按模块 ID 分开保存。两份设置确认后，再保存一次 SEQ，程序会把两个模块 ID
及其当前设置写入同名 `.modules.toml`。旧伴随文件中的原模块 ID 不会自动复制成两个新 ID。
以后加载这条 SEQ 时，两份设置按各自的模块 ID 匹配，不会互相覆盖；Load 仍不会自动
Enable 或 Apply。

!!! danger "不能让两个模块选择同一个资源"

    当前核心不会锁定 Measurement 资源，也不会阻止两个 Enabled 模块选择同一个资源 ID。
    如果两份副本都选择 `k2400_gate`，它们可能同时打开同一台仪表，造成命令交错、状态被
    相互改变或通讯超时。因此每份副本必须选择不同的物理资源。

    一个模块需要多台仪表时，它使用的全部资源也必须与其他 Enabled 模块错开。例如两份
    6221 + 2182A + 切换器模块不能共用其中任何一台仪表。

两台仪表可以连接在同一个 GPIB 控制器上，只要地址不同。它们是否能在现场稳定并行工作，
仍需用实际控制器和线缆测试；共享总线可能使一次 Measure 变慢。

## SEQ 和数据怎样变化

- 普通 `Measure` 不需要改。两份 Enabled 模块会在同一轮中测量。
- 模块自己的 SEQ 指令会分别显示在两个模块名称下面。只操作其中一台时，选择对应名称；
  两台都要操作时，分别插入两条指令。
- DAT 列会自动带模块 ID 前缀，例如 `keithley_2400_gate.Resistance(Ohm)` 和
  `keithley_2400_sample.Resistance(Ohm)`，不会重名。
- 如果模块返回 rawdata，每份副本会写自己的 rawdata 文件。

## 第一次接真实仪表时怎样检查

1. 只 Enable 第一份，确认资源、量程、输出状态和一次 `Measure` 都正确，再 Disable。
2. 只 Enable 第二份，做同样检查。
3. 两份同时 Enable，先在安全输出状态下运行一次 `Measure`。
4. 打开 DAT，确认两组列都有值，而且没有写到对方的列中。
5. 最后再逐步使用真实输出，并观察是否出现总线超时。

只要目录 ID、显示名称和物理资源各自独立，复制代码本身不会导致冲突。当前真正需要人工
防止的是“两个模块选中同一资源”。
