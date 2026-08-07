# OpenLab Measurement Module 模板

每个 `modules/<id>/` 可独立复制和发布。最小模块只有 `module.toml` 与 `backend.py`：

```toml
name = "My Meter"
version = "0.1.0"
```

```python
class Module:
    columns = {"Value": "V"}

    def open(self, api): ...
    def measure(self, slot, api): return {"Value": 1.0}
    def close(self, api): ...
```

可选接口只有 `configure(settings, api)`、`on_event(event, data, api)` 和 `slots`。
完整规范见 OpenLab Control 的 `docs/PLUGIN_DEVELOPMENT.md`；本模板中的
`simulated_transport` 是无硬件示例。

框架统一提供 PySide6、PyVISA、QtAwesome、packaging 和 typing_extensions。只有额外
依赖才写入清单，并携带离线 wheel 与带哈希锁文件。
