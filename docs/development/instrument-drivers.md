# 一台仪表一个文件

真实测量可能同时使用电流源、电压表和切换器。把所有仪表命令都塞进 `backend.py` 会很快
变得难读。简单的做法是：每台仪表一个 Python 文件，`backend.py` 只写测量顺序。

这不是强制要求。模块只有一台简单仪表时，一个 `instrument.py` 就够了。

## 推荐目录

```text
modules/my_transport/
├─ module.toml
├─ backend.py                 # 整体测量顺序
├─ frontend.py                # 可选设置窗口
├─ instruments/
│  ├─ current_source.py       # 电流源命令
│  ├─ nanovoltmeter.py        # 电压表命令
│  └─ multiplexer.py          # 切换器命令
└─ tests/
   ├─ test_instruments.py
   └─ test_measurement.py
```

不需要为了目录整齐再增加很多基类、工厂或其他中间层。

## 仪表文件只处理一台仪表

```python
class CurrentSource:
    def __init__(self, resource):
        self.resource = resource

    def identify(self):
        return self.resource.query("*IDN?").strip()

    def set_current(self, value_a, compliance_v):
        self.resource.write(f"SOUR:CURR {value_a:.12g}")
        self.resource.write(f"SENS:VOLT:PROT {compliance_v:.12g}")

    def output_off(self):
        self.resource.write("OUTP OFF")

    def close(self):
        self.resource.close()
```

这个文件负责仪表命令、返回值解析、量程、状态字和关闭连接。它不需要知道 SEQ、DAT、Qt
或其他模块。

## backend.py 只写测量顺序

```python
def open(self, api):
    self.source = CurrentSource(open_visa(self.source_address, timeout=3.0))
    self.meter = Nanovoltmeter(open_visa(self.meter_address, timeout=3.0))
    verify_supported_pair(self.source.identify(), self.meter.identify())

def measure(self, channel, api):
    api.checkpoint()
    self.switch.select(channel)
    api.sleep(self.switch_pause)
    raw = self.meter.read_samples()
    return build_result(channel, raw, self.current_a)

def close(self, api):
    try:
        if self.source is not None:
            self.source.output_off()
    finally:
        close_all(self.switch, self.meter, self.source)
```

这样打开 `backend.py` 就能直接看出：连接哪些仪表、先切换什么、等待多久、读取什么，以及
结束时怎样关闭输出。

## 每次等待都要有上限

仪表读取不能无限等待。连接、查询和读取都应设置合理时间。所有步骤加起来，还要给关闭
输出和释放连接留出时间。

写命令等待超时，不等于仪表没有执行。危险写命令不能自动再发一次；应重新连接并读回真实
状态。无法确认时停止 SEQ。

## 不接仪表也能检查命令

可以准备一个假的 VISA 连接，记录程序发送了什么：

```python
fake = FakeVisaResource(responses={"*IDN?": "MODEL,2400,..."})
source = CurrentSource(fake)
source.set_current(1e-3, 10.0)

assert fake.writes == [
    "SOUR:CURR 0.001",
    "SENS:VOLT:PROT 10",
]
```

测试应检查命令顺序和错误处理，而不只是最后算出的数字。连接真实仪表前，还要按照手册做
低风险现场检查。
