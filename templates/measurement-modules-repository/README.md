# OpenLab Measurement Module 模板

每个 `modules/<id>/` 可独立复制和发布。最小模块只有 `module.toml` 与 `backend.py`：

```toml
name = "My Meter"
version = "0.1.0"
```

```python
class Module:
    columns = {"Value": "V"}
    display_columns = ("Value",)  # 可选：在主窗口卡片显示最近值

    def open(self, api): ...
    def measure(self, slot, api): return {"Value": 1.0}
    def close(self, api): ...
```

常用可选项包括 `display_columns`、`configure(settings, api)`、
`on_event(event, data, api)`、`slots` 和模块自定义 SEQ 指令。`display_columns` 只填写
已有 DAT 列名；主窗口显示返回结果的缓存，不会额外读取仪表。完整规范见 OpenLab Control 的开发者网站与
`docs/DEVELOPMENT_REFERENCE.md`。

- `simulated_transport`：只演示 `open/measure/close` 的最小无硬件模块。
- `tutorial_resistance`：与网页教程配套的完整四通道模块，演示设置界面、数字状态码、
  rawdata、自定义普通/扫描指令及单元测试。

框架统一提供 PySide6、PyVISA、QtAwesome、packaging 和 typing_extensions。只有额外
依赖才写入清单，并携带离线 wheel 与带哈希锁文件。
