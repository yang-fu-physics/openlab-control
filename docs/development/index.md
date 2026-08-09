# 先理解扩展边界

OpenLab Control 刻意只保留两类扩展。判断方式很简单：它是在控制实验环境，还是在完成
一次测量？

| 问题 | Device Plugin | Measurement Module |
| --- | --- | --- |
| 负责对象 | 温控仪、磁体电源、只读 Monitor | 电表、电流源、切换器及组合测量方案 |
| 能否改变温度/磁场 | 主控插件可以，由核心统一授权 | 不可以，只能读取快照 |
| 生命周期 | connect / poll / set_target / hold / disconnect | open / measure / close |
| 运行位置 | 每个设备实例一个子进程 | 每个 Enabled 模块一个子进程 |
| 设置入口 | `configs/default.toml` | 模块 Settings 页与 SEQ 伴随设置 |
| 主要安全责任 | 上下限、速率、读回、Hold、失联恢复 | 仪表量程、输出、测量时序、异常清理 |

## Measurement Module 应拥有完整测量

如果一次电阻测量需要 6221、2182A 和 7001，那么这三个仪表属于同一个 Measurement
Module。模块知道切换顺序、触发状态、原始样本、compliance 和安全关断；核心只在
`Measure` 时调用它。

不要为每条 SCPI 命令增加核心接口。模块可以自由组织自己的 Python 文件，只需要在
`backend.py` 暴露一个 `Module` 类。

## Device Plugin 必须受核心约束

温度和磁场会影响所有后续指令，所以核心必须掌握：

- 哪个设备是 primary；
- 允许的目标范围和最大速率；
- 当前值、目标、速率、稳定状态与连接状态；
- 失联恢复窗；
- Stop/Error 时如何请求 Hold Current。

因此不要把温控或磁场写成普通测量模块，也不要从模块或 GUI 直接操作它们。

## 线程和进程边界

```text
GUI 主线程
├─ 主窗口、SEQ 编辑器、Data Browser
└─ 模块 Frontend QWidget（只编辑/显示数据）
        │ 请求
        ▼
后台 Runtime
├─ SEQ Engine、设备管理、DAT 唯一写入者
├─ Device Plugin 子进程 × N
└─ Enabled Measurement Module 子进程 × N
```

模块内部请求串行；同一槽位的不同模块可以并行。IPC 只接受受限 JSON，不能发送任意
Python 对象、NaN、Infinity 或仪表句柄。

## 作者不需要理解的内部实现

编写普通模块时，不需要导入 parser、engine、worker、service 或 UI 主窗口。优先只接触：

- `labcontrol.module_api.ModuleAPI`
- `labcontrol.module_api.ModuleWarning`
- `labcontrol.module_api.ModuleError`
- 可选 `labcontrol.measurement.frontend_api.ModuleUIAPI`

下一步直接创建 [第一个测量模块](first-module.md)。
