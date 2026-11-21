# 控制与安全

温度和磁场会影响整套低温系统与超导磁体的安全。编写控制类 System Instrument 的核心目标不是“出了异常尽量硬撑”，而是**在状态不明确时果断停手，并给出清晰的错误原因**。

## 1. 三层安全限值，缺一不可

扫描器生成的 `controller` 面板配置是本实验台的现场安全范围权威来源（`min_value`、`max_value`、`max_rate_per_minute`）：

1. **核心层**：在手动控制弹窗、SEQ 参数弹窗和运行时执行前统一校验边界；
2. **驱动层**：后端 `set_target()` 在发指令前必须再校验一次对应控制端点的边界；
3. **硬件层**：仪表本机限值、物理联锁和人工急停始终是最后一道防线。

```python
def set_target(self, value, rate_per_minute, mode="Settle", *, control):
    if control != "main":
        raise InstrumentError("Unknown control", "UNKNOWN_CONTROL", control)
    if not self.config.min_value <= value <= self.config.max_value:
        raise SafetyViolation("Target is outside the site limit", "TARGET_LIMIT")
    if not 0 < rate_per_minute <= self.config.max_rate_per_minute:
        raise SafetyViolation("Rate is outside the site limit", "RATE_LIMIT")
    # 校验通过后再下发仪表命令，并尽量读回确认
```

切勿在代码里暗藏比现场配置更宽松的兜底范围；缺少必要配置时直接报错拒绝启动。

## 2. `open()` 只做只读体检，不改输出

连接初始化推荐顺序：

1. 用有限超时建立连接；
2. 查询 `*IDN?` 核对型号（及必要固件信息）；
3. 核对关键接线状态（如输入通道、回路配置、单位）；
4. 读回一次当前状态；
5. **不自动写回旧设定，不改变当前目标或加热输出**；中途任一步失败必须关闭已打开句柄。

## 3. 写入后回读，超时绝不盲目重发

- 若仪表对写命令会产生响应，务必完整读走，避免留在缓冲区里导致下一次查询错位；
- 更稳妥的做法是**写入 → 立即查询 → 核对一致**（注意匹配仪表的实际数值分辨率与浮点舍入）；
- **写命令超时怎么办？** 超时并不意味着仪表没收到——可能已经开始升温或升场了。千万不要自动重发写命令！核心会将其视作“结果不确定”立即停止控制，待重新连接后先读回真实目标和速率，交由操作员判断。

## 4. `hold()` 与 `close()` 的清晰分工

- **SEQ 完成、Stop、Error 都不会调用 `hold()`**，也不会改写温控或磁体目标；
- 只有操作员在界面明确点击 **Hold Current** 时才调用 `hold()`。如果硬件需要通过“把当前值设为目标”来实现保持，必须在同一次操作里**现读新鲜数值**，绝不能用几秒前的旧缓存或 0；
- `close()` 只负责幂等释放本机通讯连接，不要擅自关加热器或发控制指令。

## 5. 断线重连与不可恢复故障

- **普通读取断线**：核心暂停依赖主温场的 SEQ 计时，按 `reconnect_interval_seconds`（默认 2 秒）自动重建子进程重连，最长持续 `reconnect_timeout_seconds`（默认 60 秒）；连回后会读回核对真实状态。
- **立即停机（不重连）**：型号不符、传感器故障报警、超出硬安全边界、写入结果不确定、以及驱动明确抛出 `SafetyViolation` 时，应立即中止，绝不反复重试。

## 6. Warning 与 Error 的使用界限

- 系统仪表的通信、控制或安全状态异常会影响整个实验，应抛出 **`InstrumentError`** 或 **`SafetyViolation`** 立即中止 SEQ；
- 只有确认完全可恢复、且不影响后续控制安全判断的提示，才使用 `InstrumentWarning`。

## 7. 跨仪表安全联动（`event_responses`）

如果需要“冷头过温报警时自动把磁体降回零场”，**不要**在驱动里跨进程去碰另一台仪表对象。正确做法是在报警源仪表的 `event_responses()` 里返回纯数据声明 `EventResponseSpec(code=..., context=..., action="zero")`：

- 核心按最高优先级通过目标磁体自己的 `set_target()` 路径以默认速率归零，保留所有限值与串行保护；
- 同一活动事件只触发一次并锁存目标（锁定期间拒绝普通 Set / Hold / 系统指令），直到源报警解除且人工复位。
