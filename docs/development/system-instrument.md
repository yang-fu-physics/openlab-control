# System Instrument 从这里开始

System Instrument 是 OpenLab Control 与实验系统之间的连接层。它负责长期存在、会影响整个
实验流程的仪表，例如：

- 主温控仪；
- 主磁场控制器；
- 只读的冷头温度、压力或液位监视仪表。

它不是 Measurement Module。电阻表、电压表、换向器以及只在 `Measure` 时工作的组合测量
方案，应写成 Measurement Module。

## 先判断该写哪一种

| 你的仪表在做什么 | 应写成 |
| --- | --- |
| SEQ 用 `Set Temperature`、`Scan Temperature` 控制它 | System Instrument |
| SEQ 用 `Set Field`、`Scan Field` 控制它 | System Instrument |
| 程序一直显示它的状态，但从不设置它 | System Instrument（Monitor） |
| 只在 `Measure` 时读取电阻、电压或电流 | Measurement Module |
| 有自己的测量通道、设置窗口或模块 SEQ 指令 | Measurement Module |

两者故意分开：System Instrument 一般随实验系统固定下来；Measurement Module 则会按实验
频繁增减。它们有不同目录、不同清单和不同生命周期。

## 它怎样工作

```text
配置中的 [[instruments]]
        │ 选择 backend + resource
        ▼
system_instruments/<id>/instrument.toml
        │ 指向 backend.py 中的类
        ▼
独立子进程 ── 串行访问 ── VISA / 串口 / 厂商库 ── 真实仪表
        │
        ├─ poll()              前面板、判稳、安全检查、状态日志
        ├─ poll_measurement()  写测量行前的即时读数（可选）
        ├─ set_target()        设置温度或磁场（可控仪表）
        └─ hold()              用新鲜读数保持当前状态（可控仪表）
```

每个配置实例使用一个独立子进程。同一实例的连接、读取和写入始终串行，不会同时向一个
VISA Session 发送两条命令。若一次完整读取已经开始，它会先读完当前完整指令；等待中的控制
或安全操作随后先执行，测量即时读取再先于尚未开始的普通后台读取。

不同实例拥有不同物理地址和不同子进程，连接与轮询会并发进行。因此目录应按物理仪表
型号/协议划分，例如 `cryocon_22c_24c`、`magnet_supply_x`，不要按 `temp_a`、`temp_b`
或“主温度/副温度”拆目录。同一地址永远只有一个实例和一个 VISA Session。

## 推荐阅读顺序

1. [写第一个 System Instrument](first-system-instrument.md)：目录、清单和最小后台。
2. [读取、前面板与日志](instrument-reading.md)：`poll()`、即时测量和附加读数。
3. [控制与安全](instrument-control-safety.md)：上下限、写入确认、Hold 和断线。
4. [测试与现场接入](instrument-testing.md)：不用真机测试、协议测试和真机门槛。

## 安装后的目录

一个 System Instrument 是一个完整文件夹：

```text
system_instruments/my_temperature_controller/
├─ instrument.toml
├─ backend.py
├─ instrument.py          # 可选：只放底层命令与响应解析
├─ requirements.lock      # 仅在确有框架外依赖时需要
└─ wheels/                # 可选：额外依赖的离线 wheel
```

核心只扫描 `system_instruments/`。模板目录 `templates/` 只是供复制和学习，不会被程序直接
加载。首阶段安装方式是手动复制完整文件夹，然后重启程序。

## 一份代码与一个实际仪表不是一回事

`instrument.toml` 描述一份型号实现；资源表描述实验室里一台实际仪表；主配置中的
`[[instruments]]` 选择它在实验系统中的角色：

```toml
# configs/instruments.local.toml
schema_version = 1

[[resources]]
id = "cryocon_main"
address = "USB0::...::INSTR"
identity = "Cryo-con,24C,..."
purpose = "system"
system_instrument = "cryocon_22c_24c"
primary_reading = "temp_b"
monitor_readings = ["temp_a"]
```

```toml
# configs/site.local.toml
[[instruments]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
role = "primary"
control_enabled = true
backend = "my_temperature_controller"
resource = "cryocon_main"
unit = "K"
min_value = 2.0
max_value = 400.0
max_rate_per_minute = 10.0
```

通讯地址放在 Git 忽略的 `configs/instruments.local.toml`；现场安全范围和 PID 表仍在
`configs/site.local.toml`。二者都不要写进公开示例。
更换实验系统中的温控仪时，通常只需复制新的 System Instrument、修改这个配置并重启，不必
为每台仪表维护一条核心分支。

## 三种 kind

- `temperature`：标准温度列，可作为主控或只读辅助温度。
- `field`：标准磁场列，可作为主控或只读辅助磁场。
- `monitor`：只有显示和日志，不参与标准温度、磁场控制。

温度或磁场最多各有一个 `role = "primary"`。只有同时满足 `primary` 和
`control_enabled = true` 的仪表，手动窗口和 SEQ 才能设置目标。其他温度、磁场实例默认
只监视；`monitor` 永远只读。

如果同一台物理仪表、同一个通讯口还能返回第二级冷头温度，不要再创建第二个连接。应在
同一个快照的有序 `metrics` 字典中加入 `InstrumentMetric`；监控卡会按字典内容自动扩展，
具体写法见[读取、前面板与日志](instrument-reading.md)。

!!! warning "示例不是通用真实驱动"

    核心模板故意在连接真实仪表前报错。只有完成型号确认、有限 I/O 超时、现场上下限、
    新鲜回读、Hold、断线恢复和硬件联锁测试后，才能用于真实实验。
