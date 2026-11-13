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

    def output_on(self):
        self.resource.write("OUTP ON")

    def output_off(self):
        self.resource.write("OUTP OFF")

    def close(self):
        self.resource.close()
```

这个文件负责仪表命令、返回值解析、量程、状态字和关闭连接。它不需要知道 SEQ、DAT、Qt
或其他模块。

## backend.py 只写测量顺序

下面是结构示意，不是可直接控制某台仪表的完整程序。`CurrentSource`、`Nanovoltmeter` 和
`Multiplexer` 分别来自上面的三个仪表文件；`open_visa`、`verify_supported_pair` 和
`close_all` 是模块作者按仪表手册实现并测试的帮助函数。示意中的地址、量程和换算规则也
必须换成该模块自己的设置。

```python
class Module:
    def __init__(self):
        self.source = None
        self.meter = None
        self.switch = None

    def open(self, api):
        try:
            self.source = CurrentSource(
                open_visa(self.source_address, timeout=3.0)
            )
            self.meter = Nanovoltmeter(
                open_visa(self.meter_address, timeout=3.0)
            )
            self.switch = Multiplexer(
                open_visa(self.switch_address, timeout=3.0)
            )
            verify_supported_pair(
                self.source.identify(), self.meter.identify()
            )
        except Exception:
            close_all(self.switch, self.meter, self.source)
            self.source = self.meter = self.switch = None
            raise

    def on_event(self, event, data, api):
        if event == "run_start":
            self.source.output_on()
            return {"Output": "On"}
        if event == "run_end":
            # completed、stopped、error 都会进入这里。
            self.source.output_off()
            return {
                "Output": "Off",
                "Last Run": data.get("reason", "—"),
            }
        return {}

    def measure(self, channel, api):
        api.checkpoint()
        self.switch.select(channel)
        api.sleep(self.switch_pause)
        voltage = self.meter.read_voltage()
        return {
            f"R{channel}": voltage / self.current_a,
            "StatusCode": 0,
        }

    def close(self, api):
        try:
            if self.source is not None:
                self.source.output_off()
        finally:
            close_all(self.switch, self.meter, self.source)
```

这样打开 `backend.py` 就能直接看出：连接哪些仪表、先切换什么、等待多久、读取什么，以及
结束时怎样处理输出。`run_end` 负责每次 SEQ 的收尾，默认关闭输出；`close` 只在 Disable
或程序退出时调用，并再次关闭输出后释放连接。关闭动作应可重复调用而不报错。

并非所有模块都要在 `run_start` 打开输出。若设置要求“每行测量完关闭”，就在
`measure` 的 `finally` 中关闭，下次调用再打开。若连续栅压等偏置必须跨 SEQ 保持，可增加
默认勾选的“SEQ 结束关闭输出”设置。取消勾选时，`run_end` 只能在读回输出和关键设置都
正确后保持，并且下一次 `run_start` 不能先关再开。任何读回不确定、Apply、Disable、
应用退出或测量异常仍要关闭；`close` 始终保证最终输出为 Off。

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
