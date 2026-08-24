# 扫描并配置仪表

这个独立工具解决一件事：找到电脑当前能看到的 VISA 地址，让你确认每个地址对应哪台物理
仪表，再把结果保存到一个本机配置文件。以后 USB 枚举或 GPIB 地址变化，只改这一份文件。

## 打开工具

源码环境：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

打包版直接双击程序根目录中的 `InstrumentScanner.exe`。它与同级的
`OpenLabControl.exe` 共享唯一的 `_internal`，所以发布包不建立 `tools/`，也不会重复携带
PySide6、PyVISA 和 Python 运行时。不要只复制扫描器 EXE；两个 EXE 都应与 `_internal`
一起保留。源码版则直接复用主项目 `.venv` 中锁定的框架依赖。

PyVISA 是 Python 接口，不是 Windows 的 VISA implementation。如果启动后的自动扫描提示无法初始化
VISA，请安装适合现场硬件的 VISA 实现；Windows 和 GPIB 环境通常使用 NI-VISA。安装后重新
打开扫描器即可，不需要重新打包 OpenLab Control。扫描失败弹窗提供可点击的
[NI-VISA 官方下载链接](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)。

默认输出是 `configs/instruments.local.toml`。该文件已被 Git 忽略。

0.16 使用 `schema_version = 2`。旧资源文件不会自动迁移；先保留旧文件作参考，再重新扫描并
人工确认。扫描器不会覆盖一个无法按当前 Schema 读取的文件。

工具打开时会自动读取这份文件，随后自动执行一次 VISA 扫描。旧资源会先显示为仪表卡片，
扫描结果再与旧表合并；没有在本次扫描中出现的旧地址也会保留，除非你明确把它的 `Use` 改为
`Ignore`。需要重新检查连接时仍可点击 **Scan VISA instruments**。

工具启动时还会自动读取同级的 `system_instruments/`：

- System Instrument 的名称、型号匹配规则和可选读数全部来自各自的 `instrument.toml`；
- 扫描器不会为了发现这些信息而导入 `backend.py`；
- 唯一匹配的 System Instrument 会自动预选；主读数由该实现固定，辅助读数由操作者勾选。

扫描器不读取 Measurement Module 目录，也不把地址绑定到某个模块。Measurement 资源保存后
会出现在模块的 Settings 中，具体地址在 Enable 模块后选择。

## 扫描会做什么

1. 窗口首次打开时调用 PyVISA 列出资源；
2. 每个地址只打开一次；
3. 设置有限的打开和读取超时；
4. 发送一次 `*IDN?`；
5. 立即关闭该通讯会话。

它不会发送 `*RST`、`clear`、输出开关、目标值、PID、量程或 Measurement Module 设置。
有些旧仪表不支持 `*IDN?`。这时地址仍会显示，Device details 会保留失败原因，你可以根据
前面板、线缆和手册人工确认。

`*IDN?` 只用于辅助认出型号。卡片顶部只显示制造商和型号；点击 **Device details** 可查看
完整返回值和扫描状态。完整内容也会原样保存，不会因为默认折叠而丢失。

!!! warning "扫描不是安全认证"

    识别成功只能说明地址在扫描时返回了一段身份文字。System Instrument 和 Measurement
    Module 在正式连接时仍必须再次核对型号、有限超时、状态和安全边界。

## 每张卡片怎样选

| 项目 | 怎样选择 |
| --- | --- |
| `Use` | 不使用选 Ignore；温度、磁场和长期监控选 System；测量仪表选 Measurement |
| `Resource name (ID)` | 自己容易记住且长期不变的名称，例如 `cryocon_main`、`keithley_2400_1` |
| `System Instrument` | 只有 System 资源需要；唯一匹配型号时自动选择，否则由操作者确认 |
| `Main reading` | 只读显示；由所选 System Instrument 清单固定 |
| `Auxiliary readings` | 勾选需要显示和记录的同一物理仪表附加读数 |

安装的 System Instrument 在 `instrument.toml` 中给出身份匹配和全部读数。例如：

```toml
main_reading = "temp_b"

[panel]
template = "controller"

[discovery]
identity_pattern = "(?i)cryo-?con.*(?:22c|24c)"

[readings.temp_b]
label = "Sample Temperature (Temp B)"
unit = "K"
decimals = 3

[readings.temp_a]
label = "Cold Head Temperature (Temp A)"
unit = "K"
decimals = 3

[readings.heater_output]
label = "Heater Output"
unit = "%FS"
decimals = 2

[readings.heater_range]
label = "Heater Range"
```

`panel.template` 选择底部主面板，`main_reading` 必须对应一个 `[readings]`。其他读数自动成为
辅助复选项。资源文件只保存勾选结果，不再重复面板、主读数、单位或显示精度。

## 为什么 TempA 和 TempB 不拆成两台

如果 TempA、TempB 来自同一个 Cryo-con 地址，它们共用一个物理通讯会话。应登记一条资源：

```toml
[[resources]]
id = "cryocon_main"
address = "USB0::...::INSTR"
purpose = "system"
system_instrument = "cryocon_22c_24c"
auxiliary_readings = ["temp_a", "heater_output", "heater_range"]
```

System Instrument 在一次 `read_status()` 中读取完整状态：TempB 写入 `value`，TempA、加热
功率和量程放在 `auxiliary` 字典中。这样不会为了显示第二个温度并发打开同一个 USB 地址；
界面把这些辅助值放入最多四格的 `readout`，不会改变底层连接数量。

如果实验室还有另一台独立温控仪或磁场电源，则为它登记另一个资源。核心会给每个不同资源
建立独立进程，可以同时连接和并发轮询。

## 保存前检查

点击 **Review changes and save** 后，工具会显示完整 TOML 和目标路径。确认：

- 每个选中地址和前面板型号一致；
- 没有把同一地址登记两次；
- System/Measurement 用途没有选反；
- 主读数正确，辅助读数选择符合实验需要；
- System Instrument 与型号一致。

保存窗口还会单独列出：

- 哪些旧资源会被替换；
- 哪些资源是新增或删除；
- 哪些旧资源保持不变。

选为 System 的卡片必须填写 `Resource name (ID)` 并选择 `System Instrument`；选为
Measurement 的卡片必须填写 `Resource name (ID)`。只要有缺项，
工具就不会写文件，而会定位并标红第一张未完成卡片。不需要的地址可以明确选 `Ignore`。

保存采用同目录临时文件再原子替换，不会留下半个 TOML。程序启动时会再次严格验证；有
未知资源、重复地址、错误用途或 System Instrument 不匹配时，会在连接真实仪表前停止。
如果旧资源文件本身无法读取，扫描器也不会覆盖它，因为这时无法可靠列出将被替换的内容。

## 主配置怎样引用

`configs/site.local.toml` 只引用稳定资源 ID：

```toml
[system_instruments]
resource_file = "configs/instruments.local.toml"

[[instruments]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
resource = "cryocon_main"
control_enabled = true
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
