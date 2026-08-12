# 扫描并配置仪表

这个独立工具解决一件事：找到电脑当前能看到的 VISA 地址，让你确认每个地址对应哪台物理
仪表，再把结果保存到一个本机配置文件。以后 USB 枚举或 GPIB 地址变化，只改这一份文件。

## 打开工具

源码环境：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

打包版直接双击程序目录中的 `tools\InstrumentScanner.exe`。Release 的 `tools/` 只包含
这个独立 EXE，不包含 Python 源码；因此可以单独复制使用。源码版则直接复用主项目
`.venv` 中锁定的 PySide6、PyVISA 及其他框架依赖。

PyVISA 是 Python 接口，不是 Windows 的 VISA implementation。如果点击扫描时提示无法初始化
VISA，请安装适合现场硬件的 VISA 实现；Windows 和 GPIB 环境通常使用 NI-VISA。安装后重新
打开扫描器即可，不需要重新打包 OpenLab Control。

默认输出是 `configs/instruments.local.toml`。该文件已被 Git 忽略。

## 扫描会做什么

1. 调用 PyVISA 列出资源；
2. 每个地址只打开一次；
3. 设置有限的打开和读取超时；
4. 发送一次 `*IDN?`；
5. 立即关闭该通讯会话。

它不会发送 `*RST`、`clear`、输出开关、目标值、PID、量程或 Measurement Module 设置。
有些旧仪表不支持 `*IDN?`。这时地址仍会显示，Scan status 会保留失败原因，你可以根据
前面板、线缆和手册人工确认。

!!! warning "扫描不是安全认证"

    识别成功只能说明地址在扫描时返回了一段身份文字。System Instrument 和 Measurement
    Module 在正式连接时仍必须再次核对型号、有限超时、状态和安全边界。

## 每一列怎样选

| 列 | 怎样填写 |
| --- | --- |
| `Use` | 不使用选 Ignore；温度、磁场和长期监控选 System；测量仪表选 Measurement |
| `Resource ID` | 自己容易记住且长期不变的名称，例如 `cryocon_main`、`keithley_2400_1` |
| `System Instrument` | 只有 System 资源需要；选择与真实型号对应的实现 |
| `Primary reading` | System Instrument 的主控制/主记录值，例如样品温度 `temp_b` |
| `Monitor readings` | 同一物理仪表的辅助读数，用逗号分开，例如 `temp_a` |

安装的 System Instrument 可以在 `instrument.toml` 的 `[discovery]` 中给出身份匹配和通道
建议。唯一匹配时工具会预填，但不会跳过人工确认。

## 为什么 TempA 和 TempB 不拆成两台

如果 TempA、TempB 来自同一个 Cryo-con 地址，它们共用一个物理通讯会话。应登记一条资源：

```toml
[[resources]]
id = "cryocon_main"
address = "USB0::...::INSTR"
purpose = "system"
system_instrument = "cryocon_22c_24c"
primary_reading = "temp_b"
monitor_readings = ["temp_a"]
```

System Instrument 在一次 `poll()` 中读取完整状态：TempB 作为快照主值，TempA、加热功率和
量程放在 `metrics` 字典中。这样不会为了显示第二个温度并发打开同一个 USB 地址。

如果实验室还有另一台独立温控仪或磁场电源，则为它登记另一个资源。核心会给每个不同资源
建立独立进程，可以同时连接和并发轮询。

## 保存前检查

点击 **Preview and Save** 后，工具会显示完整 TOML 和目标路径。确认：

- 每个选中地址和前面板型号一致；
- 没有把同一地址登记两次；
- System/Measurement 用途没有选反；
- 主读数和辅助读数没有重复；
- System Instrument 与型号一致。

保存采用同目录临时文件再原子替换，不会留下半个 TOML。程序启动时会再次严格验证；有
未知资源、重复地址、错误用途或 System Instrument 不匹配时，会在连接真实仪表前停止。

## 主配置怎样引用

`configs/site.local.toml` 只引用稳定资源 ID：

```toml
[system_instruments]
resource_file = "configs/instruments.local.toml"

[[instruments]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
backend = "cryocon_22c_24c"
resource = "cryocon_main"
role = "primary"
control_enabled = true
unit = "K"
min_value = 2.0
max_value = 400.0
max_rate_per_minute = 10.0
```

每次 Run 会同时保存 `configuration.toml` 和解析后的 `instrument-resources.toml`，便于日后
核对当时实际使用的地址。

## Measurement Module 怎样使用

Measurement Module 不需要自己再扫描全部 VISA。设置界面可以读取：

```python
resources = api.resources()
for resource_id, info in resources.items():
    combo.addItem(f"{resource_id} — {info['identity']}", resource_id)
```

模块保存 `resource_id`，不是原始地址。后台在 `open` 或 `configure` 中同样调用
`api.resource_address(resource_id)`。这样配置窗口和
后台使用的是同一份只读快照，也不会让第三方模块修改核心资源表。
