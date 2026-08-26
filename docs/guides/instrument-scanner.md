# 扫描并配置仪表

Instrument Scanner 把“电脑看到哪些 VISA 地址”和“OpenLab Control 要怎样使用每台 System
Instrument”放在一个向导里。它不会修改 `configs/general.toml`。

保存结果分开存放：

```text
configs/visa.resources.toml              未分配的 VISA，供 Measurement Module 使用
configs/instruments/<instrument-id>.toml System Instrument 实例和面板选择
configs/pid/<instance-id>.toml           某个实例自己的 PID 数据
```

全新安装不必先创建这些文件。没有 System 面板时 OpenLab Control 仍能启动；三个仿真选项也
都默认关闭。

## 打开工具

源码环境：

```powershell
.\.venv\Scripts\python.exe .\tools\instrument_scanner.py
```

Windows 发布包直接双击根目录中的 `InstrumentScanner.exe`。它和
`OpenLabControl.exe` 共用同一个 `_internal`；不要单独移动其中一个 EXE。

PyVISA 只是 Python 接口。连接 GPIB、USB 或其他 VISA 硬件时，还要安装与接口匹配的 VISA
Runtime。若初始化失败，可使用弹窗中的
[NI-VISA 官方下载链接](https://www.ni.com/en/support/downloads/drivers/download-ni-visa.html)，
或安装接口厂商提供的 Runtime，然后重新打开扫描器。

## 向导的三个部分

左侧步骤按固定顺序排列：

1. **VISA Resources**：扫描地址，决定哪些地址留给 Measurement Module。
2. **每种 System Instrument 一页**：添加一个或多个物理实例，并确认该型号自己的字段与
   固定面板。
3. **Review & Save**：选择可选仿真、调整全部已开启面板的顺序、检查完整写入预览。

System Instrument 的名称、字段、面板、读数和身份匹配规则都来自
`system_instruments/<id>/instrument.toml`。扫描器只读取清单，不会为了显示表单而导入
`backend.py`。

## VISA 扫描会做什么

点击 **Scan VISA Resources** 后，扫描器会：

1. 让 PyVISA 列出当前资源；
2. 每个地址只打开一次，并设置有限超时；
3. 发送一次 `*IDN?`；
4. 记录完整返回值或失败原因；
5. 立即关闭临时会话。

它不会发送复位、清除、输出开关、目标值、PID、量程或模块设置。某台仪表没有返回
`*IDN?` 时，地址仍会显示；操作者必须根据仪表面板、线缆和手册确认它。

!!! warning "身份文字不是安全认证"

    `*IDN?` 只帮助识别型号。System Instrument 或 Measurement Module 正式连接时仍要核对
    型号、状态、有限超时和安全边界。

## VISA 资源页怎样选

每张卡片包含地址、身份、Resource ID 和 **Keep for Measurement Module**：

- 新扫描到的地址默认保留给 Measurement Module，可修改稳定的 Resource ID。
- 曾经保存、但本次没有发现的 Measurement 地址会灰显为 **Not detected**，并默认继续
  保留；取消勾选才会从 `visa.resources.toml` 删除。
- 某个地址被 System Instrument 实例选中后，Measurement 勾选会自动关闭并禁用。
- 同一个 VISA 地址只能分配一次，不能同时被两个 System 实例或 System 与 Measurement
  使用。

这里的“默认保留”只适用于未分配的 Measurement VISA。旧 System Instrument 实例若使用的
地址本次没有检测到，会显示 **Not detected — this instance will not be saved**，并从最终生成
配置排除；System 页面只保存本次已经检测并完成配置的 VISA 实例。

Resource ID 使用小写字母、数字和下划线，并以字母开头，例如 `keithley_2400_1`。Measurement
Module 保存这个 ID，再由框架解析地址；模块不需要自己做一遍全盘 VISA 扫描。

## 添加 System Instrument 实例

每个已安装的模板有自己的步骤。点击 **Add Instrument** 后填写唯一的 Instance ID；同一种
型号可以添加多台物理实例，每个实例有独立后台进程和通讯会话。

有 `discovery.identity_pattern` 的模板会显示 **VISA Resource** 下拉框。只有本次确实发现的
地址可选，身份唯一匹配时会标出匹配结果。分配给实例的地址和身份写入该实例，不会写进
`visa.resources.toml`。

没有 VISA 发现规则的仪表使用自己的清单字段。例如网络仪表可在 `config_fields` 中声明
`host` 和 `port`，扫描器会在该仪表页面显示对应输入。连接方式、默认值和范围由这一型号的
作者定义，VISA 资源页不负责它。

### 型号专用字段

API v4 清单可声明以下输入类型：

| `type` | 扫描器控件 |
| --- | --- |
| `string` | 文字框 |
| `integer` | 整数框，可带 `min`/`max` |
| `number` | 数值框，可带 `min`/`max` |
| `boolean` | 复选框 |
| `choice` | `options` 下拉框 |
| `pid_file` | 第一次配置时选择复制来源 |

每个字段都有作者给出的 `default`。请按该型号说明确认实际通道、协议超时和其他选项；不要
把清单默认值当作现场验证。

### 固定面板

作者在 `[[panels]]` 中定义可以出现的面板。扫描器可以关闭、开启和排序这些固定面板，但
不能临时增加另一种面板。四种模板是：

- `controller`：当前值、目标、速率和稳定状态；
- `readout`：一个只读值；
- `readout_grid`：一到四个只读值；
- `switch`：一个状态值和清单声明的无参数指令按钮。

开启面板后要选择角色：

- `none`：只显示、记录或提供按钮；
- `sample_temp`：由 Temperature/Scan Temperature 使用；
- `field`：由 Field/Scan Field 使用。

只有 `controller` 能使用 `sample_temp` 或 `field`，并且模板本身必须支持相应 kind。
`sample_temp` 和 `field` 各自全局最多一个；`none` 可以重复。一个 `controller` 还要确认主
读数、上下限、默认/最大速率和稳定参数。其他面板只选择开启状态与顺序，角色保持
`none`。

所有开启面板共用一个全局顺序。最后一页可用 **Move Up/Move Down** 调整，保存时会写成
从 1 开始连续的 `order`。

## PID 文件第一次怎样建立

若某个模板有 `type = "pid_file"` 的字段，第一次保存实例时会把作者指定的示例复制为：

```text
configs/pid/<instance-id>.toml
```

第一次也可以点击 **Choose PID File…**，选择另一份已经验证的 TOML 作为复制来源。目标文件
存在后，扫描器只显示“existing file will be preserved”，不会再让保存操作换来源。

以后再次保存或删除这个实例，扫描器都不会覆盖或删除 PID 文件。作者示例可能故意不能
直接运行；Cryo-con 22C/24C 的示例含 `zones = []`，应用会在连接前拒绝空区间。先填入该
冷却系统已经验证的 PID 数据，再启动 OpenLab Control。

## 可选仿真与空配置

最后一页提供：

- Simulated Temperature；
- Simulated Magnetic Field；
- Simulated 2nd Stage。

三项默认都不勾选。勾选后才生成各自的 API v4 文件，并加入全局面板顺序。温度仿真自动
使用 `sample_temp`，磁场仿真自动使用 `field`，因此不能与已有同名角色同时启用。全部不选
也是有效结果；主程序会以没有 System 面板的状态启动。

## 保存前检查完整预览

进入 **Review & Save** 后，预览会列出：

- `CREATE`：新文件；
- `OVERWRITE`：将被完整替换的文件；
- `UNCHANGED`：内容相同；
- `DELETE`：不在当前选择中的生成文件；
- `CREATE PID`：只在目标 PID 文件不存在时复制。

还要检查完整的 `visa.resources.toml` 和每个 System Instrument 文件内容。未填完的实例不会
保存，页面会明确列出其名称。

保存不是局部追加。确认后会原子写入预览中的全部生成文件，用当前选择完整替换
`visa.resources.toml`，并删除标为 DELETE 的其他 `configs/instruments/*.toml`。现有
`configs/pid/*.toml` 始终不覆盖、不删除。

因此每次保存前都要检查全部实例、面板、仿真与顺序，而不只检查刚改的一项。OpenLab
Control 启动时还会再次验证重复 ID、重复地址、角色、顺序、引用和范围；有问题会在连接真
实仪表前停止。

## Measurement Module 怎样读取资源

扫描器只建立未分配 VISA 的清单，不绑定某个 Measurement Module。模块设置窗口可以读取：

```python
resources = api.resources()
for resource_id, info in resources.items():
    combo.addItem(f"{resource_id} — {info['identity']}", resource_id)
```

模块保存 `resource_id`，后台再调用 `api.resource_address(resource_id)`。设置窗口与后台使用
同一份只读快照，也不会让第三方模块修改资源文件。
