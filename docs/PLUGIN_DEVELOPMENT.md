# 设备与插件完整开发工作流

本文档是后续修改设备、增加温控仪、磁体电源、Keithley、Lake Shore 372 或其他仪器的标准流程。

## 1. 先定义设备契约

在写代码前记录：

- 品牌、完整型号、固件版本和选件。
- 通信方式：VISA/GPIB、串口、TCP、USB、COM/.NET 或厂商 SDK。
- 单位和分辨率。
- 查询指令、设置指令、完成状态和错误队列。
- 设备允许的上下限及速率。
- 通信超时和合理重试次数。
- 断线、超时、过载、开路、联锁等状态属于 Warning 还是 Error。
- Hold 的准确厂商语义。
- 哪些安全功能由硬件完成，软件不得绕过。

把这些信息作为插件目录中的 `README.md`，并保存对应厂商手册版本。

## 2. 选择能力类型

- 温度控制：`kind = "temperature"`，实现 Poll、Set Target、Hold。
- 磁场控制：`kind = "field"`，实现 Poll、Set Target、Hold。
- 测量仪器：`kind = "measurement"`，实现 Poll、Measure。
- 只读单值监视：`kind = "monitor"`，只实现 Poll；例如 `2nd Stage` 温度。

不要用品牌或型号扩展 SequenceEngine。SEQ 只面向能力和逻辑设备 ID。

默认磁场能力以 Oe 与框架交换数值，状态、SEQ 和 DAT 使用两位小数；温度以 K 使用三位小数。若厂商 API 使用 T，插件应在协议边界按 `1 T = 10000 Oe` 换算目标、读数和速率，且配置中的上下限、判稳容差与斜率阈值必须全部采用框架原生单位。只有明确把该逻辑设备的 `unit` 配置为 T 时，才可直接向框架返回 T。

## 3. 建立插件文件

复制：

- 控制器：`plugin_templates/controller_plugin.py`
- 测量设备：`plugin_templates/measurement_plugin.py`
- 只读监视器：`plugin_templates/monitor_plugin.py`

到：

```text
src/labcontrol_plugins/<your_plugin>.py
```

例如：

```text
src/labcontrol_plugins/lakeshore372.py
```

类名使用明确型号：

```python
class LakeShore372(DevicePlugin):
    api_version = "1.0"
```

## 4. 隔离通信层

推荐把协议通信和框架适配分开：

```text
LakeShore372 DevicePlugin
└─ LakeShore372Transport
   ├─ write(command)
   ├─ query(command)
   ├─ reconnect()
   └─ close()
```

好处：

- 可用假 Transport 做单元测试。
- VISA、串口和 TCP 细节不会泄漏到框架。
- 可单独验证指令编码、终止符和超时。

若厂商库是同步阻塞 API，使用：

```python
result = await asyncio.to_thread(blocking_function, argument)
```

不要在异步方法里直接执行长时间阻塞调用，也不要使用无限等待。

## 5. 实现生命周期

### `__init__`

只保存配置和建立内存状态。禁止：

- 打开 VISA/串口。
- 向设备发送命令。
- 修改设备输出。
- 启动不可停止的后台线程。

### `connect`

允许：

- 建立通信。
- 查询 `*IDN?` 或等效身份。
- 验证型号和固件。
- 读取现状。

除非用户配置明确要求，否则不得在连接时重置、归零或改变目标。

### `poll`

返回 `DeviceSnapshot`：

- `timestamp` 必须使用 `time.monotonic()`。
- 数值单位必须等于设备配置的 `unit`。
- 控制设备必须返回 `current`、`target` 和 `rate_per_minute`。
- 测量设备返回最近通道值。
- Monitor 只返回 `current`，不得伪造 `target`、`rate_per_minute` 或 `stability`。
- `stability` 不由插件填写，中央算法会覆盖。

Monitor 不实现 `set_target()`、`hold()` 或 `measure()`。框架会在调用插件前拒绝目标设置；真实插件也不得通过其他入口改变仪表输出。若同一台温控仪同时提供主控温和辅助温度，建议分别配置一个 `temperature` 逻辑设备和一个 `monitor` 逻辑设备，并让具体插件/通信层安全共享同一物理会话。

### `set_target`

框架已经检查通用上下限和速率。插件仍需检查：

- 厂商模式限制。
- 当前硬件联锁。
- 特定量程和方向限制。
- 单位换算后的合法性。

### `hold`

默认要求保持中止瞬间的当前值。推荐流程：

1. 获取新鲜当前读数。
2. 验证读数有效。
3. 向设备发出厂商定义的 Hold 或把目标设为当前值。
4. 读取返回状态确认。

读数无效时必须抛出 Error，不能猜测或默认归零。

### `measure`

返回：

```python
{"R1": 1.23, "R2": 4.56}
```

键必须与配置 `channels` 对应。缺测值使用 `None`，不要使用字符串、NaN 文本或静默复用旧值。

`context` 包含最近温度、磁场和其他设备快照，可用于选择量程或记录同步信息，但插件不得修改这些快照。

## 6. 配置插件

在 TOML 中加入：

```toml
[[devices]]
id = "bridge"
display_name = "Lake Shore 372"
kind = "measurement"
plugin = "labcontrol_plugins.lakeshore372:LakeShore372"
unit = "Ohm"
channels = ["R1", "R2", "R3", "R4"]
visa_resource = "GPIB0::12::INSTR"
timeout_ms = 5000
retry_count = 2
```

框架不认识的字段自动进入 `config.extras`。

无需修改：

- 主界面。
- StatusTile。
- SequenceEngine。
- DAT Writer。

重启后状态块会按配置自动出现。

## 7. 错误映射

插件异常必须映射为稳定代码：

```python
raise DeviceWarning("一次读取超时", "READ_TIMEOUT", channel)
raise DeviceError("设备联锁断开", "INTERLOCK_OPEN", interlock_name)
```

选择准则：

- Warning：本次数据可缺失或可以安全继续，设备仍处于已知安全状态。
- Error：状态未知、控制失败、数据不可置信或继续可能危险。

不要把异常字符串本身当代码。代码和 context 决定去重；文字可以包含当前数值。

当后续 Poll 或 Measure 成功时，DeviceManager 会自动解除此前同类操作产生的活动事件。反复失败只弹一次，恢复后再次失败会重新弹窗。

## 8. 重试策略

仅对满足以下条件的操作重试：

- 幂等查询。
- 厂商明确允许重复的设置。
- 能确认第一次命令未执行或重复执行无害。

推荐退避：短等待、有限次数、总时间受限。禁止无限重试。最终失败按契约上升为 Warning 或 Error。

## 9. 单元测试

复制 `plugin_templates/test_plugin.py`，至少覆盖：

- 身份识别正确和错误型号。
- Connect/Disconnect 可重复调用。
- 指令终止符、编码和解析。
- Poll 单位与时间戳。
- 上下限附近的设置。
- 最大速率。
- 超时映射。
- 设备错误队列映射。
- Warning 重复和恢复。
- Hold 使用当前值而非零。
- Measure 缺失通道返回 None。

测试应使用假 Transport，不要求实验室硬件。

运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 10. 仿真与回放

真实连接前，为插件准备：

- 正常响应样本。
- 慢响应。
- 格式错误响应。
- 超时。
- 断线后恢复。
- 设备 Warning。
- 设备 Error。

优先把厂商原始响应保存为脱敏测试 fixture，驱动解析器回放。不要把实验数据、网络凭据或序列号提交到公共仓库。

## 11. 真实硬件分阶段上线

1. 只连接，不发设置命令。
2. 连续 Poll，核对单位和刷新周期。
3. 在安全范围中心执行一个很小的设置变化。
4. 验证达到目标、数值判稳和超时。
5. 测试人工 Hold。
6. 测试 Pause。
7. 测试 Stop。
8. 人工制造可恢复 Warning。
9. 在厂商允许条件下验证 Error 路径。
10. 运行最小 SEQ。
11. 运行嵌套 SEQ。
12. 核对 DAT、事件日志和设备面板。

任何一步失败，回到仿真或假 Transport 层修复，不扩大真实硬件动作范围。

## 12. 发布插件

发布前记录：

- 插件版本。
- `api_version`。
- 支持型号、固件和接口。
- 默认超时和重试。
- 错误代码表。
- 配置字段表。
- 已执行的硬件验证清单。
- 已知限制。

源代码运行时，新模块只需位于 `src/labcontrol_plugins`。若使用已打包 EXE，需要重新执行打包，让 PyInstaller 收集新模块。

## 13. 修改现有插件

标准流程：

1. 复制当前配置和一个短 SEQ 作为回归 fixture。
2. 新增失败测试，证明现有问题。
3. 只修改目标插件或其 Transport。
4. 运行全部自动测试。
5. 运行无界面仿真。
6. 在受控硬件环境执行最小变化测试。
7. 更新插件版本、错误代码表和 Changelog。
8. 重新打包并保留旧版本回退包。

禁止直接在实验运行期间修改插件文件或热重载驱动。

## 14. 完成定义

一个设备插件只有在以下条件全部满足时才算完成：

- 无导入副作用。
- 所有 I/O 有超时。
- 单设备访问串行化。
- 单位明确并有测试。
- 安全限制配置化。
- Hold 经过实机验证。
- Warning/Error 代码稳定且可解除。
- 仿真、单测和实机清单全部通过。
- 配置、使用和故障处理文档齐全。
