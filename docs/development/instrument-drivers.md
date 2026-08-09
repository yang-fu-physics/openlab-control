# 把仪表指令放进独立文件

核心不强制模块内部结构，但真实组合仪表建议“一台仪表一个 Python 文件”。这样
`backend.py` 只描述实验流程，SCPI/状态字/量程表可以独立测试。

## 推荐结构

```text
modules/my_transport/
├─ module.toml
├─ backend.py              # open/configure/measure/close 与跨仪表编排
├─ frontend.py             # 可选 QWidget
├─ instruments/
│  ├─ current_source.py    # 一台仪表一个文件
│  ├─ nanovoltmeter.py
│  └─ multiplexer.py
└─ tests/
   ├─ test_protocol.py
   ├─ test_backend.py
   └─ fakes.py
```

如果模块只有一台简单仪表，也可以直接使用 `instrument.py`。不要为了形式增加基类、Mixin、
工厂或依赖注入容器。

## 驱动文件负责什么

```python
class CurrentSource:
    def __init__(self, resource):
        self.resource = resource

    def identify(self) -> str:
        return self.resource.query("*IDN?").strip()

    def configure_current(self, value_a: float, compliance_v: float) -> None:
        self.resource.write(f"SOUR:CURR {value_a:.12g}")
        self.resource.write(f"SENS:VOLT:PROT {compliance_v:.12g}")

    def output_off(self) -> None:
        self.resource.write("OUTP OFF")

    def close(self) -> None:
        self.resource.close()
```

驱动文件应负责：

- 精确仪表命令和响应解析；
- 状态字、量程、compliance 和错误队列；
- 仪表本机配置读回；
- 单个会话的关闭和尽力安全状态。

它不应知道 SEQ、slot、DAT、Qt 或温场控制。

## backend.py 负责编排

```python
def open(self, api):
    self.source = CurrentSource(open_visa(self.source_address, timeout=3.0))
    self.meter = Nanovoltmeter(open_visa(self.meter_address, timeout=3.0))
    verify_supported_pair(self.source.identify(), self.meter.identify())

def measure(self, slot, api):
    api.checkpoint()
    self.switch.select(slot)
    api.sleep(self.switch_pause)
    raw = self.meter.read_delta_samples()
    return build_result(slot, raw, self.current_a)

def close(self, api):
    try:
        if self.source is not None:
            self.source.output_off()
    finally:
        close_all(self.switch, self.meter, self.source)
```

## Timeout 的真正边界

`api.timeout` 是核心给整次操作的总时间，不是 VISA timeout。最坏路径必须满足：

```text
所有可能耗尽的 I/O timeout 总和
+ pause / dwell / arm 等待
+ 输出关闭和资源释放预留
< api.timeout
```

写命令 timeout 表示“可能已经执行”，禁止自动重发危险写命令。应断开、重新连接并读取
真实状态；无法确认时抛 Error。

## 用假会话测试协议

协议测试要断言完整命令顺序，而不只是最终返回值：

```python
fake = FakeVisaResource(responses={"*IDN?": "MODEL,2400,..."})
driver = CurrentSource(fake)
driver.configure_current(1e-3, 10.0)

assert fake.writes == [
    "SOUR:CURR 0.001",
    "SENS:VOLT:PROT 10",
]
```

真实仪表手册属于驱动测试的依据，不应把未经验证的命令直接复制到生产模块。
