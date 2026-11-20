# 一台仪表一个文件（接入真实硬件的组织方式）

真实电输运实验常常同时用到两三台仪器（例如电流源 + 纳伏表 + 切换器）。如果把所有 SCPI 字符串和状态机逻辑全塞进 `backend.py`，代码很快就会变得难以维护。

最清晰的做法是**按职责拆开**：

- **仪表文件（如 `keithley_6221.py`、`keithley_2182a.py`）**：只管那一台仪表的命令拼接、回包解析和串口/VISA 通信，完全不知道 SEQ、Qt 或 `ModuleAPI`。
- **`backend.py`**：只管整体流程编排——连接顺序、通道切换、等待稳定、超时预算、告警分级和异常收尾。

## 推荐目录布局

```text
modules/my_transport/
├─ module.toml
├─ backend.py                 # 流程编排与生命周期
├─ frontend.py                # 可选：设置与状态界面
├─ keithley_6221.py           # 电流源协议
├─ keithley_2182a.py          # 纳伏表协议
└─ keithley_7001.py           # 切换器协议
```

不需要为了架构“好看”硬造一堆基类、工厂或抽象层；简单单仪表模块放一个 `my_meter.py` 就足够清爽。

## 1. 仪表文件只写协议与解析

VISA 连接可以直接复用核心提供的 `labcontrol.measurement.visa.VisaSession`（只释放自己的仪表句柄，不会误关进程内共享的 VISA ResourceManager）：

```python
class CurrentSource:
    def __init__(self, session):
        self.session = session

    def identify(self):
        return self.session.query("*IDN?").strip()

    def set_current(self, value_a, compliance_v):
        self.session.write(f"SOUR:CURR {value_a:.12g}")
        self.session.write(f"SENS:VOLT:PROT {compliance_v:.12g}")

    def output_on(self):
        self.session.write("OUTP ON")

    def output_off(self):
        self.session.write("OUTP OFF")

    def close(self):
        self.session.close()
```

## 2. `backend.py` 只写清晰的测量步骤

下面是多仪表编排的结构示意（具体命令、回读与安全边界请以对应仪表的真实手册为准）：

```python
class Module:
    def __init__(self):
        self.source = None
        self.meter = None
        self.switch = None

    def open(self, api):
        # 仅核对可用资源，不自动改写仪表设置；也可在 configure() 中打开连接
        api.resources()

    def on_event(self, event, data, api):
        if event == "run_start":
            self.source.output_on()
            return {"Output": "On"}
        if event == "run_end":
            # completed / stopped / error 三种情况都会进入这里
            self.source.output_off()
            return {"Output": "Off", "Last Run": data.get("reason", "—")}
        return {}

    def measure(self, channel, api):
        api.checkpoint()
        self.switch.select(channel)
        api.sleep(self.switch_pause)
        voltage = self.meter.read_voltage()
        return {f"R{channel}": voltage / self.current_a, "StatusCode": 0}

    def close(self, api):
        # 必须幂等，且任何一台关闭失败时仍继续释放其余会话
        try:
            if self.source is not None:
                self.source.output_off()
        finally:
            close_all(self.switch, self.meter, self.source)
            self.source = self.meter = self.switch = None
```

关于输出保持与关闭的三种常见模式：

1. **逐行关闭（最保守）**：每次 `measure()` 开头开输出、末尾 `finally` 关输出；
2. **整轮保持、`run_end` 关闭（常见默认）**：在 `run_start` 开输出或测量时保持，每次 SEQ 结束/停止/报错在 `run_end` 关闭；
3. **跨 SEQ 连续偏置（如连续栅压）**：提供**默认勾选**的“SEQ 结束关闭输出”开关；只有用户明确取消勾选，且 `run_end` 读回确认输出和关键配置完全一致时才保持，下一次 `run_start` 不做先关再开。Disable、重新 Apply、异常或读回不确定时仍须强制关闭。

## 3. 超时与写失败原则

- 每次 VISA / 串口 / 网络调用都要设有限 I/O 超时，并给 `output_off()` 与资源释放留出余量，使其总时间小于 `api.timeout`。
- 写命令如果超时，**无法判断仪表到底执行了没有**；千万不要自动重发，应中止当前操作并重新读回真实状态。

## 4. 不接真机也能测协议

把仪表文件独立出来后，只需写一个极简的 `FakeVisaSession` 就能在普通电脑上验证完整命令顺序与边界错误：

```python
fake = FakeVisaSession(responses={"*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,..."})
source = CurrentSource(fake)
source.set_current(1e-3, 10.0)

assert fake.writes == [
    "SOUR:CURR 0.001",
    "SENS:VOLT:PROT 10",
]
```

协议逻辑在假连接下全部通过后，再按 [仪表安全清单](../guides/safety-checklist.md) 接假负载进行真机验证。
