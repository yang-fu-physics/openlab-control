# 控制与安全

温度和磁场会影响整个实验系统。System Instrument 的目标不是“尽量继续”，而是在状态不清楚
时停止继续写入，并让操作者看到明确原因。

## 谁可以控制

要让手动窗口和 SEQ 控制一台仪表，配置必须同时满足：

```toml
kind = "temperature"       # 或 field
control_enabled = true
```

同一种 `kind` 最多一个 `control_enabled = true` 的实例；其他实例和 `monitor` 只读。核心会
在界面、SEQ 预检和实际执行三处检查控制权，不只依赖按钮是否灰色。

## 上下限至少检查两次

主配置是现场安全范围的权威来源：

```toml
min_value = 2.0
max_value = 400.0
max_rate_per_minute = 10.0
operation_timeout_seconds = 10.0
shutdown_timeout_seconds = 3.0
```

核心用这些值限制手动窗口、SEQ 参数窗口和运行时写入。后台的 `set_target()` 在发送命令前
还要再次检查；仪表面板限制和硬件联锁是最后一层。任何一层都不能替代另外两层。

```python
def set_target(self, value, rate_per_minute, mode="Settle"):
    if not self.config.min_value <= value <= self.config.max_value:
        raise SafetyViolation("Target is outside the site limit", "TARGET_LIMIT")
    if not 0 < rate_per_minute <= self.config.max_rate_per_minute:
        raise SafetyViolation("Rate is outside the site limit", "RATE_LIMIT")
    # 之后才允许发送仪表命令，并读取实际设置确认。
```

不要在代码中偷偷放一个比主配置更宽的默认范围。缺少必要现场参数时，连接应失败关闭。

## `open()` 只确认，不设置

启动时应依次：

1. 用有限 I/O 超时打开连接；
2. 查询型号和必要的固件信息；
3. 确认接线相关的输入、回路和单位；
4. 读一次当前值、目标和状态；
5. 不自动应用保存设置，不改变目标或输出。

部分初始化失败时，也必须关闭已经打开的句柄。发现清单不等于连接仪表；只有配置实际选择
该资源时，核心才会启动它。

## 写入后读取确认

若仪表在写命令后会留下响应，必须把这次响应读完，否则下一次查询可能读到错行。更安全的
做法是“写入 + 查询实际值 + 比较”：

```text
设置速率 → 查询速率 → 核对
设置目标 → 查询目标 → 核对
```

如果一次危险写入等待超时，不能自动重发。此时无法知道仪表是否已经执行，核心会把它视为
结果不确定的写入并停止继续控制。重新连接后先读取实际目标和速率，再由操作者决定下一步。

## `hold()` 必须使用新鲜读数

停止 SEQ 后，温度和磁场保持当前状态，不自动回零。若仪表需要通过“把当前值设为新目标”
实现 Hold，必须先在同一次操作里读取真实当前值并验证，再写一次；不能使用几秒前的缓存、
猜测值或零。

`close()` 只负责释放通讯资源，不应擅自关闭输出或改变设定。具体安全状态应由仪表本机
保护、硬件联锁和已经明确实现的 `hold()` 共同保证。

## 断线与重连

普通通讯失联时，核心暂停依赖主温度或主磁场的 SEQ 计时，并按
`system_instruments.reconnect_interval_seconds` 尝试恢复。超过
`reconnect_timeout_seconds`（默认 60 秒）仍无法恢复，才转为故障。

重新连接后必须读取并核对仪表实际状态；不能假定断线前的缓存仍然成立。以下情况不应进入
普通重试，而应立即停止：

- 型号或接线不符；
- 安全传感器报警；
- 当前值超出硬边界；
- 写入结果不确定；
- 后台明确抛出 `SafetyViolation`。

## Error 与 Warning

System Instrument 的连接、控制或安全状态出问题，会影响整个实验，因此通常使用
`InstrumentError` 或 `SafetyViolation`，活动 SEQ 会中止。只有确实可恢复且不会让后续控制
失去安全依据的情况，才使用 `InstrumentWarning`。

Measurement Module 的单个异常测量值是另一回事：它可以按模块自己的规则把该测量行留空、
写状态码并报告 Warning，而不必停止 SEQ。不要把测量数据异常规则直接套到温度或磁场控制。

## 真机前仍需要硬件保护

进程隔离、软件上下限和超时不能替代限流、限压、过温、磁体保护、联锁或人工急停。第一次
接入请从只读身份确认开始，在安全负载和低风险范围内逐步开放写入。
