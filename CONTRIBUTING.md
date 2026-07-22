# Contributing

## 修改原则

- 保持控制核心和 GUI 解耦。
- 不在设备插件导入或构造阶段执行 I/O。
- 不绕过 `DeviceManager` 直接从 GUI 操作设备。
- 新增指令时同时更新 Parser、Formatter、Engine、参数 Spec 和测试。
- 新增告警使用稳定的 source/code/context。
- 所有安全限制必须配置化并有边界测试。

## 提交前检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe run.py --headless-demo --timeout 120
```

更新：

- `CHANGELOG.md`
- 受影响的技术文档
- 插件版本和支持矩阵

真实设备修改还必须执行 `docs/TEST_PLAN.md` 的相关硬件检查。
