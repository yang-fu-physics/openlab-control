# Tutorial Resistance

开发者网站配套的无硬件四通道模块。它有意保持单文件后端，同时覆盖完整公开接口：

- `open / configure / measure / on_event / close`；
- `slots = 4`，每个逻辑通道独立写一行；
- 无效电阻留空，`StatusCode` 使用数字；
- 返回有限原始采样序列，由核心写入 rawdata sidecar；
- Settings/Status QWidget；
- Enable 后注册 Set Current 和 Scan Current 两条 SEQ 指令。

它只模拟电阻和激励电流，不连接真实仪表，也不能作为真实仪表安全实现。
