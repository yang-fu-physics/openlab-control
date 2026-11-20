# 测试与现场接入

把“代码逻辑错”和“现场接线/通讯错”分开排查，最省时间也最安全：**先用假连接把命令顺序和异常路径测透，再接假负载低风险实测，最后才上真实实验系统**。

## 1. 离线单元测试至少覆盖四类场景

**连接与清单**

- `instrument.toml` 通过 API v4 校验，所有 `config_fields`、`controls`、`panels`、`readings` 和 `sequence_commands` 引用正确；
- 正确 `*IDN?` 成功连接并读到首份有效状态；错误型号立即拒绝且不发任何写命令；
- 打开到一半失败能干净关闭句柄；连续两次调用 `close()` 不报错。

**读取与解析**

- 正常响应解析为正确单位的有限数值；
- 空响应、乱码、`NaN` / `Infinity`、仪表错误码被明确拦截；
- 若实现了 `read_measurement()`，确认只发预期查询且不混入旧附加值；
- `auxiliary` 键集合固定，未读取项为 `None`。

**控制与边界**

- 最小值、最大值、最大速率边界刚好放行；越界、零速率、负速率、`NaN` 在发送前拒绝；
- 命令发送顺序（PID / 速率 / 目标）与手册一致，写入回读被正确消费和比对；
- 写命令超时不自动重发；
- `hold()` 先读新鲜值再写入保持命令。

**故障与配置拦截**

- VISA 地址冲突、角色重复、缺少/空 PID 文件在连接真机前即报错；
- 普通读失败进入重连、超恢复窗转故障；安全报警（`SafetyViolation`）立即停机不重试。

## 2. 用假通讯对象检查完整命令序列

把底层通讯协议单独拆进 `instrument.py` 后，只需几十行 `FakeTransport` 就能验证发出去的每一条指令和顺序：

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

断言 `fake.commands` 的完整列表，能立刻发现漏读回包、设温与设速率顺序颠倒、失败后多发命令等隐患。

## 3. 在本机联调扫描器与主程序

在系统仪表仓库根目录跑完单元测试：

```powershell
<OpenLabControl>\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

随后将完整目录复制到 `OpenLabControl/system_instruments/` 并检查：

- Instrument Scanner 能正确渲染全部字段和固定面板，保存后的生成文件能被主程序顺利加载；
- 支持同型号多台 `[[instances]]`，且每个实例只占一个进程和一个会话；
- 分配给系统仪表的 VISA 地址不再出现在 `configs/visa.resources.toml`；
- 再次保存不覆盖或删除已有的 `configs/pid/<实例ID>.toml`；
- 前面板、Live Trend、实验 DAT 与 `instrument_status.dat` 字段一致。

## 4. 分阶段真机放行

1. **只读**：只核对地址、型号、固件、单位和状态位；
2. **假负载**：做最低风险单次写入并立即与仪表面板比对；
3. **边界**：核对软件限值、仪表本机限值与硬件联锁三层保护；
4. **故障演练**：拔线、超时、制造异常响应、测试断线重连和退出清理；
5. **长跑观察**：观察句柄、进程和内存是否平稳；
6. **记录归档**：写明已测型号、固件、接线、范围与 PID 参数；未完成前继续标注 Beta，不要无人值守运行。
