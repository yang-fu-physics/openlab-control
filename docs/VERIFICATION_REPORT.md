# OpenLab Control 0.9.0 验证报告

- 验证日期：2026-07-22
- 验证平台：Windows 11 x64（build 26200）
- 源码运行时：Python 3.13.2、PySide6 6.11.1
- 打包工具：PyInstaller 6.21.0（onedir）

## 结论

0.9.0 仿真框架达到本阶段交付条件：Scan Temperature 已支持 Linear/List 点位，显式列表的输入、单行 SEQ、嵌套、执行顺序、重复点和整表安全预检均完成验证；此前的 Oe 磁场、温度/磁场精度、英文界面、分辨率缩放、`2nd Stage`、SEQ 多行编辑与窗口恢复、Data Browser、日志、Warning/Error、判稳和中止保持继续通过回归测试。

本结论只适用于仿真设备。由于尚未接入真实仪表，它不构成温控仪、磁体电源或测量仪器的硬件验收。

## 自动测试

运行命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：51 项通过，0 项失败。

覆盖内容：

- 用户 SEQ 模板解析和文字往返保留。
- 禁用 `F` 行及禁用嵌套 Scan 的解析、序列化与执行跳过。
- SEQ Ctrl/Shift 多行选择、批量 Ctrl+D/Ctrl+E/Delete/Ctrl+C/Ctrl+V、稳定文档顺序和完整 Scan 深复制。
- 父 Scan、后代和 End Scan 同时选择时的结构去重，以及批量粘贴后的多项选择恢复。
- 运行期编辑锁禁止变更操作，同时允许 Copy。
- 任意层级 Scan 的抽象语法树结构。
- Scan Temperature 参数窗口在 Linear/List 间切换，并只显示对应的点位字段。
- 温度 List 单行语法解析、原文往返、三位小数规范化及空项错误拒绝。
- 非单调温度 List 和重复点严格按声明顺序执行；后部目标越界时在首次移动前拒绝整份列表。
- 温度扫描、磁场扫描和测量的嵌套执行。
- 设备插件加载、单位换算和安全限值拒绝。
- 默认 Oe 场配置与原物理边界等价；温度三位、Oe 两位精度覆盖状态卡、参数窗口、SEQ 与 DAT。
- Oe/T 参数切换同步换算目标与速率；旧 T 命令继续使用六位小数。
- 并发轮询旧快照不会覆盖新目标，固定小数格式消除负零。
- 容差、窗口斜率、持续时间与超时判稳。
- Warning 锁存去重、恢复和继续执行。
- Error 中止、阻止后续测量和保持当前温度/磁场。
- DAT 表头、LabVIEW 时间戳、稀疏通道行和事件恢复记录。
- 用户原始 DAT 的 2,458 行、数值列、空单元格和四个稀疏电阻通道解析。
- DAT 拖入、浏览点到完整源行的映射、默认轴、近邻点选择以及文件追加后的自动刷新。
- 自动刷新时保留人工设置的框选缩放范围。
- Overlay 多 Y 共享范围，Stacked 多图共享 X 且各自保存 Y 范围。
- Y 多选窗口连续勾选、至少一个 Y 约束及一次应用语义。
- X/Y Log10 等对数间距、非正值排除、状态标识和跨窗口恢复。
- `.plt` v2 尺度往返、v1 Linear 兼容、同名规范路径、附加文件名兼容、格式拒绝和自动保存。
- `2nd Stage` Monitor 的连接、只读数值、无 Target/稳定性、非控制光标与双击信号抑制，以及核心目标拒绝。
- 长 SEQ 触发水平滚动范围后，编辑器重建仍回到行首。
- 主窗口缩小到 1180×720 时，SEQ 和 Data Browser 浮动窗口均保持在中央工作区内。
- 关闭 SEQ 子窗口后点击 New，保留的 MDI 外框和内部编辑器一起恢复，`End Sequence` 可见且窗口重新激活。
- 1366×768、1080p、2K、4K 自动倍率，以及 1.40× 手动覆盖和越界配置拒绝。

## 端到端仿真

源码版本与打包版本均运行 `examples/nested_scan.seq`。该序列执行：

1. 设定温度。
2. 两点扫温。
3. 每个温度点执行三点扫场。
4. 每个磁场点测量四通道输运数据。
5. 完成三点扫时间测量。

两次运行均进入 `Completed`，并生成配置快照、SEQ 快照、`experiment.dat` 和 `events.dat`。打包版本进程退出码为 0。

此外，用户提供的原始 `template_original.seq` 已按原文运行完成：接受仿真 Initialize、将另一台电脑上的绝对数据路径安全重定向至本次运行目录，并在 60 秒内完成 60 个测量点，最终进入 `Completed`。四通道稀疏写入共得到 240 行数据，与预期一致。

源码和 Windows 发布 EXE 还分别运行 `examples/temperature_list.seq`，两次均进入 `Completed`。生成 DAT 的 SequenceStep 依次记录 `300.000 → 299.900 → 299.500 → 299.900 K`，证明非单调顺序和重复点在打包边界后仍被保留；DAT Header 的 BYAPP 版本为 0.9.0。

## GUI 与发布包验证

- 源码 GUI 在 Qt 离屏平台完成启动、四个设备轮询、窗口渲染、截图和正常关闭；`2nd Stage` 显示 `Monitoring` 与只读说明。
- Windows 发布 EXE 独立完成同一流程，退出码为 0；QtAwesome 字体和图标资源已由 PyInstaller hook 收集。
- 人工检查发布 EXE 截图：英文菜单、矢量工具栏、左侧 Sequence Control、中间单行 SEQ、右侧命令栏以及底部温度/磁场/测量卡片均正常显示。
- 主界面磁场状态卡显示 Oe 与两位小数，温度状态卡显示三位小数；默认嵌套 SEQ 使用 Oe。
- 人工检查 1180×720 截图：SEQ 子窗口行首完整，四个状态卡片无裁切；自动布局测试同时验证 SEQ 与 Data Browser 的窗口边界。
- 人工检查强制 1.40× 截图：全局字体约 14pt，工具栏、状态卡片、SEQ、Data Browser 三幅共享 X 图及坐标标签均完整；状态栏正确显示 `1.40x (Manual)`。
- 人工检查 SEQ 编辑截图：两行可同时保持选择；禁用命令和受禁用 Scan 影响的子命令均为灰色删除线；右键菜单显示 Disable、Enable、Delete、Copy、Paste 及对应快捷键。
- Data Browser 在源码和发布 EXE 中均成功加载用户模板的 2,458 行，自动套用示例 `.plt`，显示三幅共享 X 的纵向子图和 0.75 秒刷新状态；Y 多选窗口保持打开供连续勾选，X/Y Linear/Logarithmic 菜单可独立切换。
- 发布 EXE 运行 `disabled_commands.seq` 退出码为 0，日志包含两条 `STEP_SKIPPED_DISABLED`，未执行被禁用的 Set Temperature 和 Scan Field。
- 发布 EXE 运行 `temperature_list.seq` 退出码为 0，四个列表点和四轮子 Measure 顺序正确。
- 发布 EXE 的无界面完整序列验证退出码为 0。

主窗口验收图：`docs/main-window-preview.png`。  
SEQ 编辑验收图：`docs/sequence-context-menu-preview.png`。  
数据浏览器验收图：`docs/data-browser-preview.png`。

## 本地打包复验

PyInstaller 重新生成 `dist/OpenLabControl` 后完成以下独立验证：

- Windows EXE 运行完整无界面嵌套序列，退出码 0。
- Windows EXE 运行显式温度 List 序列，退出码 0，DAT 版本与点位顺序正确。
- EXE 运行带普通禁用行和禁用 Scan 子树的示例，退出码 0。
- EXE 完成离屏 GUI 冒烟并显示全部 QtAwesome 工具栏图标，退出码 0。
- EXE 打开 2,458 行示例 DAT 及同名 `.plt`，三幅共享 X 子图正常渲染，退出码 0。
- EXE 分别使用 `ui_scale = "auto"` 和 `ui_scale = 1.4` 启动，状态栏显示正确模式与倍率，两次离屏截图均成功。

## 尚未验证

- 任何真实 GPIB、VISA、串口、以太网或厂商 SDK 通信。
- 实际磁体和低温系统的硬件联锁。
- 断电、线缆脱落、驱动进程崩溃等物理故障。
- 长时间（数小时至数天）运行、磁盘写满和系统休眠。

接入真实设备前，必须逐项完成 `docs/TEST_PLAN.md` 的上线清单，并在设备插件配置中采用保守的上下限和速率。
