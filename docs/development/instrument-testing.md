# 测试与现场接入

先在没有真机的情况下证明命令顺序和异常路径，再连接安全负载，最后才接实验系统。这样能把
“代码逻辑错”和“现场通讯错”分开。

## 最少要测什么

### 连接

- `instrument.toml` 使用 API v4，`config_fields`、`controls`、`panels`、`readings` 和
  `sequence_commands` 的 ID 与引用全部有效。
- 正确型号可以连接并读到第一份有效状态。
- 错误型号立即拒绝，且不发送任何设置命令。
- 打开通讯后任一步失败，句柄仍会关闭。
- `close()` 调用两次也不会报错。

### 读取

- 正常响应能解析为正确单位和有限数值。
- 空响应、乱码、`NaN`、无穷、仪表错误码都被拒绝。
- `read_measurement()` 只发送预期的即时读取，不混入旧的附加值。
- `auxiliary` 的 key 和顺序始终固定；未读取值为 `None`。

### 控制

- 每个 `controller` 都引用已声明的 control 和 reading；扫描器生成的现场限制能传到后台。
- 最小值、最大值、最大速率边界恰好可用。
- 边界外、零速率、负速率、`NaN` 和无穷在发送前被拒绝。
- 设置 PID、速率和目标的顺序与仪表手册一致。
- 每次写入后的回读都会消费，并检查容差。
- 写入超时不会自动重发。
- Hold 先读取当前值，再发送一次保持命令。

### 故障

- 同一 VISA 地址重复分配、面板顺序不连续、两个 `sample_temp` 或两个 `field` 会在连接前
  被拒绝。
- `pid_file` 缺失、损坏或含空 `zones` 时，依赖这些区间的实现会在连接前明确失败。
- 普通读失败进入重连，恢复后核对实际状态。
- 超过恢复时间转为故障。
- 安全报警不会被当成普通通讯错误重试。
- 应用退出、进程强杀和部分初始化都能释放句柄。

## 用假的通讯对象测试命令顺序

底层仪表命令最好放在独立的 `instrument.py`，后台的 `backend.py` 只保留
`open/read_status/read_measurement/set_target/hold/execute_sequence_command/close`。假的通讯对象按顺序返回手册中的
响应，并记录收到的命令：

```python
class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.commands = []
        self.closed = False

    def query(self, command):
        self.commands.append(command)
        if not self.replies:
            raise TimeoutError("no reply")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True
```

测试不仅比较最后结果，还要比较 `commands` 的完整顺序。这样能发现漏读响应、设定顺序反了、
失败后又重发等问题。

## 仓库中的参考测试

核心示例位于：

```text
system_instruments/
├─ example_controller/
└─ example_monitor/
```

模板后台故意不能控制真实仪表。复制后请为具体型号新增协议测试，不要把“能 import”当成
真机验证。

## 本地检查命令

在 System Instrument 仓库根目录运行：

```text
<OpenLabControl>\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

再把一个副本手动复制到核心的 `system_instruments/`，使用仿真或假的通讯后端检查：

- Instrument Scanner 能从作者清单生成表单，并把除面板模板外的静态元数据写入
  `configs/instruments/<id>.toml`；
- 同一种型号可以保存多个 `[[instances]]`，每个实例仍只有一个进程和通讯会话；
- 分配给 System Instrument 的 VISA 地址不会留在 `configs/visa.resources.toml`；
- 三个仿真默认关闭，没有 System 面板时主程序仍可启动；
- 再次保存会按最终预览完整覆盖生成配置，但不覆盖或删除现有 PID 文件；
- 源码版与 Windows 打包版发现结果一致；
- 所需 Python 包已包含在核心锁定依赖中；
- 前面板、Live Trend、实验 DAT 和 `instrument_status.dat` 字段一致；
- Stop、Error、退出和重建窗口不会增加通讯会话。

## 真机分阶段门槛

1. **只读**：确认 VISA 地址、型号、固件、单位和状态位。
2. **安全负载**：只测试最小风险的一次写入，并立即回读。
3. **边界**：验证软件、仪表本机和硬件三层限制。
4. **故障注入**：拔线、超时、错响应、进程退出和重新连接。
5. **长时间运行**：检查句柄、线程、进程和内存是否持续增长。
6. **现场签字**：记录型号、固件、接线、允许范围、PID/斜坡配置和硬件联锁结果。

任何一项未完成，都应继续标为未经过真机验证，不建议无人值守运行。
