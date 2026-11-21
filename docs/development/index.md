# 写测量模块（Measurement Module）从这里开始

**测量模块**就是一段负责“完成一次测量”的独立 Python 程序——例如切通道、给样品通电流、等仪表稳定、读回电压并换算成电阻。只要 SEQ 执行到 `Measure`，主程序就会自动调用它。

如果你只想接入电阻、电压、电流或锁相测量，读这一章就够了，不需要关心温控仪和磁体控制器。

## 职责边界：什么归模块，什么归主程序？

| 归测量模块管 | 归主程序管 |
| --- | --- |
| 连接和控制自己的测量仪表（可包含多台，如 6221 + 2182A + 切换器） | 温度、磁场控制与 SEQ 调度 |
| 应用量程、电流、等待时间等设置 | 统一把结果写入实验 `.dat` 与 `rawdata/` |
| 按当前通道（slot）执行一次测量并返回数值与数字状态码 | 进程隔离、Pause/Stop 检查、多模块并行 |
| 在每次 SEQ 结束（`run_end`）时安全收尾输出，在 `close` 时释放连接 | 保存设置文件、锁定运行期界面、左侧卡片展示 |

> **一条原则**：完成同一种测量需要多台仪表配合时（例如 6221 电流源 + 2182A 纳伏表 + 7001 切换器），把它们放进**同一个模块**里统一编排，不要拆成三个模块。

## 目录只需要两到三个文件

```text
modules/my_measurement/
├─ module.toml    # 只有模块显示名和版本号
├─ backend.py     # 连接、配置、测量与收尾（核心逻辑）
└─ frontend.py    # 可选：自定义设置与状态窗口
```

初学者建议先只写 `module.toml` 和 `backend.py`，跑通数据写入后再决定要不要加界面。

## 生命周期：主程序何时调用你的方法？

```text
用户在 Modules 勾选 Enable
        ↓
open(api)            建立初始状态（不连接或只做只读准备，不自动写旧设置）
        ↓
用户核对参数并点击 Apply Settings
        ↓
configure(settings, api)   可选：校验参数、写入仪表并回读确认
        ↓
点击 Run → on_event("run_start", {}, api)
        ↓
SEQ 每次执行 Measure（按通道槽位依次调用）
        ↓
measure(slot, api)   测当前通道并返回一行字典
        ↓
SEQ 完成 / Stop / 报错
        ↓
on_event("run_end", {"reason": ...}, api)   默认关闭输出（completed / stopped / error 都会触发）
        ↓
用户取消勾选 Disable 或退出软件
        ↓
close(api)           再次确认输出关闭并释放仪表连接（必须可重复调用）
```

!!! warning "会开输出的真实模块必须实现 `run_end`"

    一次 SEQ 跑完后，模块依然保持 Enabled，主程序**不会**调用 `close()`。
    因此凡是会打开电流/电压输出的真实模块，都必须在 `on_event("run_end", ...)` 中完成收尾：
    默认在 `completed`、`stopped`、`error` 三种情况下都关闭输出；只有确实需要跨 SEQ 保持连续偏置（如栅压）时，
    才提供默认勾选的“SEQ 结束关闭输出”开关，并在保持前回读确认。`close()` 则作为 Disable 和程序退出时的最终保障。

## 推荐学习顺序

1. [写第一个测量模块](first-module.md) — 5 分钟跑通四通道示例并亲手写出最小模块
2. [让每个通道各写一行](results-and-slots.md) — 理解多通道槽位（`slots`）、空列、状态码与原始数据
3. [按需添加设置与状态窗口](frontend.md) — 给模块加上 PySide6 参数页与状态页
4. [一台仪表一个文件](instrument-drivers.md) — 接入真实仪表时如何拆分协议文件与编排逻辑
5. [多台相同仪表复用一个模块](reuse-identical-instruments.md) — 现场同时用两台同型号源表时如何配置
6. [模块自己的 SEQ 指令](sequence-commands.md) — 需要 `Set Current` / `Scan Current` 等专属命令时再看

想查阅完整字段规范，可随时翻看 [完整开发规范](../DEVELOPMENT_REFERENCE.md) 与 [公共 Python API](../reference/public-api.md)。
