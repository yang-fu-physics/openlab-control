# 从这里开始写测量模块

测量模块就是一段完成测量的小程序。例如：切换通道、等待仪表稳定、读取电压，再换算成
电阻。主程序会在 SEQ 遇到 `Measure` 时调用它。

如果你只是想写电阻、电压或电流测量，下面这些内容已经够用了。本章不讲主程序管理的
其他设备。

## 模块负责什么

- 用户点击 Enable 后，连接模块自己的仪表；
- 用户点击 Apply Settings 后，应用量程、电流、等待时间等设置；
- 收到测量请求后，按顺序完成一次测量；
- 返回数字结果和状态码；
- 每次 SEQ 完成、Stop 或 Error 时，关闭本次 Run 打开的输出；
- Disable 或程序退出时，再释放仪表连接。

一个测量需要多台仪表时，把它们放进同一个模块。例如 6221、2182A 和 7001 一起完成
Delta 测量，就应该是一个模块，而不是三个模块。

## 模块不负责什么

- 不改变主程序管理的其他设备；
- 不直接写 DAT 文件；
- 不控制 SEQ 开始、暂停或停止；
- 不从设置窗口直接连接仪表；
- 不要求修改主程序才能增加一种新仪表。

## 先认识三个文件

```text
modules/my_measurement/
├─ module.toml    # 模块名称和版本
├─ backend.py     # 连接、测量、关闭
└─ frontend.py    # 可选：设置和状态窗口
```

初学时只创建前两个文件。模块已经能测量后，再决定是否需要 `frontend.py`。

## 程序会按这个顺序调用

```text
用户点击 Enable
        ↓
open：连接仪表
        ↓
用户检查设置并点击 Apply Settings
        ↓
configure：应用设置（如果模块有设置）
        ↓
SEQ 开始 → run_start
        ↓
SEQ 执行 Measure（可执行多次）
        ↓
measure：逐通道测量
        ↓
SEQ 完成 / Stop / Error
        ↓
run_end：默认关闭；或按明确设置保持已验证的输出
        ↓
用户点击 Disable 或程序退出
        ↓
close：再次确认输出关闭并释放连接
```

最小仿真模块只需 `open`、`measure`、`close`。只要真实模块可能在两次 `Measure` 之间
保持电流、电压或其他输出，就必须实现 `on_event`：在 `run_start` 准备本次运行，在
`run_end` 完成本轮收尾。默认关闭输出；只有用户明确取消默认安全选项时，才可在读回确认
输出和关键设置后保持连续偏置。`run_end` 的 reason 是 `completed`、`stopped` 或
`error`；三种情况都要执行收尾。`close` 只在 Disable 或应用退出时调用，不能代替
`run_end`，并且始终要关闭输出。

## 推荐阅读顺序

1. [第一个测量模块](first-module.md)
2. [多通道数据](results-and-slots.md)
3. [设置与状态窗口](frontend.md)（需要设置时再看）
4. [一台仪表一个文件](instrument-drivers.md)（连接真实仪表前再看）
5. [模块自己的 SEQ 指令](sequence-commands.md)（确实需要时再看）

更完整、也更偏技术的内容放在 [完整扩展规范](../PLUGIN_DEVELOPMENT.md) 中。初学者不需要先读。
