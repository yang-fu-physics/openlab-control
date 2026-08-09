# 开发温度、磁场或 Monitor 插件

Device Plugin 的接口比 Measurement Module 更严格，因为核心必须持续读取设备并统一执行
安全限制、角色、稳定性和失联恢复。

## 清单

```text
device_plugins/my_controller/
├─ device.toml
└─ backend.py
```

```toml
id = "my_controller"
name = "My Temperature Controller"
version = "0.1.0"
api_version = "1.0"
core_requires = ">=0.13,<0.14"
backend = "backend:MyController"
kinds = ["temperature"]
dependencies = []
```

`kinds` 可以是 `temperature`、`field` 或 `monitor`。不同设备协议、安全行为和界面不同，
核心只提供 fail-closed 示例，不声称示例可以直接控制任意真实仪表。

## 后端接口

```python
from labcontrol.devices.base import DevicePlugin


class MyController(DevicePlugin):
    async def connect(self): ...
    async def poll(self): ...
    async def set_target(self, value, rate_per_minute, mode="Settle"): ...
    async def hold(self): ...
    async def disconnect(self): ...
```

只读 Monitor 只需实现 `connect/poll/disconnect`；默认 `set_target` 和 `hold` 会明确拒绝。

## connect：只确认，不改变输出

`connect()` 应当：

1. 使用有限 timeout 打开会话；
2. 查询 `*IDN?` 或等价身份；
3. 验证型号和必要固件；
4. 读取当前状态；
5. 不自动写入保存配置，不改变目标或输出。

导入、构造和发现阶段都不得连接仪表。

## poll：返回完整快照

```python
return DeviceSnapshot(
    device_id=self.config.id,
    display_name=self.config.display_name,
    kind=self.config.kind,
    timestamp=time.monotonic(),
    connected=True,
    unit=self.config.unit,
    current=current,
    target=target,
    rate_per_minute=rate,
    activity=DeviceActivity.MOVING,
)
```

时间戳必须使用单调时钟。绝对 DAT 时间由日志层生成，不要混用。

## 三层限制

目标和速率至少在三处防护：

1. 核心主配置限制手动控制、SEQ 参数窗口和运行时执行；
2. 插件在发送命令前再次检查 `config.min_value/max_value/max_rate_per_minute`；
3. 仪表本机限制、联锁和急停作为最终硬件边界。

配置示例：

```toml
[[devices]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
role = "primary"
plugin = "my_controller"
unit = "K"
min_value = 1.8
max_value = 400.0
max_rate_per_minute = 20.0
address = "GPIB0::12::INSTR"
```

同一种类最多一个 primary；其他设备默认只读监视。更换设备只需复制目标插件、修改这一段
配置并重启，不需要为每台温控或磁体维护核心分支。

## Hold 和失联

`hold()` 必须基于新鲜读回保持当前值，不能使用缓存值、猜测值或零。写命令 timeout 不自动
重试。读链路失联时核心关闭旧 worker 并重连；恢复后读取实际 target/rate，不重放旧写入。

完整 fail-closed 骨架位于：

- `plugin_templates/device-plugins-repository/plugins/example_controller/`
- `plugin_templates/device-plugins-repository/plugins/example_monitor/`

在连接真实设备前完成 [测试策略](testing.md) 和 [安全清单](../guides/safety-checklist.md)。
