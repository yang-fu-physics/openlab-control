# 操作手册

## 安装与启动

### Windows 发布包

解压整个 `OpenLabControl` 文件夹后运行：

```text
OpenLabControl.exe
```

不要只复制 EXE；`configs/`、`modules/`、`docs/` 等目录必须和它一起保留。首次启动所有测量模块都是 Disabled，这是固定安全行为，不会恢复上次 Enable 状态。

### 源码运行

```text
setup.bat
run.bat
```

或：

```text
.venv\Scripts\python.exe run.py
```

指定配置/SEQ：

```text
.venv\Scripts\python.exe run.py --config configs\default.toml --sequence examples\nested_scan.seq
```

单独打开 DAT Browser：

```text
.venv\Scripts\python.exe run.py --data-file C:\Data\sample.dat
```

无界面验证模块运行时，可显式重复使用 `--enable-module ID`：

```text
.venv\Scripts\python.exe run.py --headless-demo --enable-module simulated_transport --sequence examples\module_measurement.seq --timeout 30
```

这个参数只服务于自动验证；正常 GUI 启动不会恢复 Enabled 状态，仍需用户在 Modules Manager 手动启用。

## 主窗口

- 左侧 `Sequence Control`：数据文件、当前 SEQ、运行状态和 Run/Pause/Stop。
- 中央：浮动 SEQ 和 Data Browser 窗口。
- 右侧 `Sequence Command Bar`：双击命令后设置参数并插入。
- 底部 `Device Status`：Temperature、Magnetic Field、`2nd Stage` 等控制/Monitor；不再显示测量 Transport 块。
- 工具栏 `Modules`：测量模块管理。
- `Run Log`：Warning、Error、步骤和模块手动动作记录，可从 View 菜单显示。

温度显示三位小数；Oe 显示两位。温度/磁场状态块双击打开手动控制，Monitor 只显示，不弹控制窗口。

## 测量模块

### Enable

1. 点击工具栏或菜单 `Modules`。
2. 管理器只有 `Enabled / Name / Version` 三列。
3. 勾选所需模块。
4. 程序显示 `Initializing <module>...`；初始化成功后才真正勾选并打开模块窗口。
5. 初始化失败会弹 Error，仍保持 Disabled。

模块窗口是独立浮动 Windows 窗口：可移动、最小化，保持在主窗口之前但不全局置顶；用户不能用关闭按钮/Alt+F4 关闭。双击管理器中的 Enabled 模块可把窗口带到前面。

### Settings 与 Status

- 默认打开 `Settings` 页。
- Enable 会加载上次保存参数，但不会把这些值发送给仪表。
- `Apply Settings` 只显示在 `Settings` 页；切换到 `Status` 后不会显示。
- 检查参数后点击 `Apply Settings`，再次确认后才发送。
- `Status` 页显示连接、实际状态、输出状态、读数等，布局由模块自己定义。
- 模块可以提供 `Test Connection`、`Read Now`、`Measure Now`；仅 SEQ Idle 可用，结果不写实验 DAT。

模块窗口设置了按 UI Scale 缩放的最小尺寸；缩小到完整显示页签、参数区和操作区的边界后不能继续缩小。

Settings 在 Apply、Disable、关闭程序和 Run 前自动保存。设置保存在 `module_data/<id>/settings.toml`，不修改模块源码。

### Run 前未 Apply

如果 Settings 页面有修改但尚未 Apply，Run 会询问：

- `Apply and Run`：先等待所有修改成功 Apply，再启动。
- `Run Without Applying`：仪表保持现状，但把当前界面值作为 desired settings 保存到运行目录。
- `Cancel`：不运行。

### Disable

取消勾选时：

1. 先保存 Settings；
2. 调用模块 abort；
3. abort 成功后才取消勾选、关闭工作进程并隐藏窗口。

abort 失败时模块仍显示 Enabled，窗口保持打开并显示 Faulted/Status，程序不会假装已经安全禁用。

### Refresh 与依赖

只有 SEQ Idle 且全部模块 Disabled 时可以 `Refresh`。它重新扫描 `modules/`，不做运行中的热替换。

共享依赖安装同样要求全部模块 Disabled，防止工作进程正在导入或使用某个包时替换其文件。

依赖缺失时：

1. 先 Disable 全部模块，再选中目标模块。
2. 点击 `Install Dependencies`。
3. 确认安装到共享环境。
4. 程序先尝试根 `wheels/` 和模块 `wheels/`。
5. 离线失败后，如确需联网，再确认 Online Install。
6. 完成后 Refresh。

依赖版本冲突不能靠 Enable 绕过，需要模块开发者统一版本。

## 编辑 SEQ

### 新建/打开/保存

- File → New/Open/Save/Save As。
- 关闭浮动 SEQ 后，点击 New/Open/Edit 会重新显示现有编辑器。
- 文件扩展名是 `.seq`。

### 插入与修改

- 右侧命令双击：弹参数窗口，确认后插入。
- SEQ 行双击：编辑该行参数。
- 插入在 Scan/End Scan 上时会进入该 Scan；否则插在所选命令之后。
- 所有温场弹窗直接显示配置中的上下限和最大速率。

### 多行操作

可 Ctrl/Shift 选择多行，然后右键或键盘：

- Disable / Enable
- Delete
- Copy / Paste

选中完整 Scan 的父/子混合范围时，结构操作只处理最外层节点，避免重复。Running 时禁止修改，Copy 仍可用。

### Measure

Measurement Command 只有：

```text
T Measure
```

不在命令中写模块名、重复次数或间隔。模块选择由 Run 前的 Enabled 状态决定；重复/间隔用 Scan Time 或嵌套 Scan 表达。

## 运行

运行前建议：

1. 检查 Temperature/Field/2nd Stage 状态。
2. Enable 所需模块并检查 Status。
3. 确认需 Apply 的 Settings 已发送。
4. 保存或核对 SEQ；Run 会另外保存实际执行快照。
5. 确认数据文件位置和磁盘空间。
6. 点击 Run。

Run 开始后：

- SEQ、模块 Enable/Disable/Refresh、Settings/Apply 和手动动作锁定；
- 所有 Enabled 模块成为本次固定 Schema；
- `begin_sequence()` 在第一条指令前执行；
- 每个 Measure 并行调用所有模块并等待全部结束；
- 最终按 completed/stopped/error 调用每个模块的 `end_sequence()`。

### Pause

Pause 在安全检查点暂停 SEQ 调度；不会主动关闭模块输出，也不会断开设备。Resume 继续。

### Stop

Stop 后：

- 温度和磁场按配置默认 Hold Current；
- 模块执行 `end_sequence("stopped")`；
- 不执行模块 abort，模块仍 Enabled，窗口保持可用。

## 手动温场控制

双击底部 Temperature 或 Magnetic Field：

- Target；
- Rate；
- Settle/Sweep；
- `Set`；
- `Hold Current`。

弹窗使用配置上下限和最大速率。`2nd Stage` 等 Monitor 没有手动控制。

## Warning 与 Error

### Warning

- 弹窗标题 `Warning / Operation Continues`；
- SEQ 继续；
- 有效温场和模块数据照常写 DAT；
- 详细 code/context 写 events.dat；
- 同一 Source+Code+Context 活动期间只弹一次。

典型：测量超量程、接近范围、某一点无效。

### Error

- 弹窗标题 `Error / Operation Stopped`；
- Running/Paused SEQ 进入 Faulted；
- 温场执行 Hold；
- 模块执行 `end_sequence("error")`，不调用 abort；
- 已写数据保留。

典型：设备掉线、互锁、二级冷头过温、源表硬件报警、模块 Schema 违规。

如果同一故障被轮询/多次测量反复报告，只会更新计数，不会连续弹窗轰炸。恢复后事件 RESOLVED；再次发生才重新弹。

## Data Browser

Data Browser 与当前实验 DAT 解耦：

1. Graph → Data Browser，或把 `.dat` 拖入主窗口。
2. 它只跟踪明确打开的文件。
3. 文件追加后自动刷新图。

右键图区域可：

- 选择 X；
- 一次勾选多个 Y，再统一确认；
- Overlay（一个图多个 Y）或 Stacked（多图共享 X）；
- X Axis Log / Y Axis Log；
- Reset Zoom 等。

鼠标框选放大；双击最近数据点弹出该原始行完整字段。显示布局保存在 DAT 同目录同名 `.plt`，下次打开自动恢复。

## 输出与备份

一次 Run 至少保留：

```text
sequence.seq
configuration.toml
module_settings/*.settings.toml
module_settings/*.status-at-start.json
experiment.dat
events.dat
```

建议实验结束后整体复制整个运行目录，而不是只复制 DAT。模块 desired 设置和实际 Status 对复现实验同样重要。

## 关闭程序

SEQ 运行中关闭主窗口会确认；确认后请求 Stop/Hold/End。随后：

1. 保存所有 Enabled 模块 Settings；
2. 对每个 Enabled 模块调用 abort；
3. 断开温场/Monitor；
4. 关闭日志与应用。

下次启动所有模块仍是 Disabled，但 Settings 值会在下次 Enable 时自动载入且不自动 Apply。

## 常见问题

### 模块无法勾选

选中行查看底部说明。常见原因：清单 Invalid、API 版本不匹配、依赖缺失或模块间依赖冲突。先 Disable 全部模块，再修复并 Refresh。

### 发布包提示没有 Python Runtime

Install Dependencies 需要配置 `modules.python_executable` 或放置 `runtime/python/python.exe`。程序运行本身不要求该便携 Python；只有安装新依赖需要。

### Enable 后参数没有作用

这是设计行为。Enable 只初始化并加载保存值，必须点击 `Apply Settings` 并确认才发送。

### 模块窗口关不掉

这是安全设计。先在 Modules Manager 取消 Enable；abort 成功后窗口自动隐藏。

### Disable 后仍然 Enabled

abort 失败。查看 Status 和 Error，不要强制假定输出已关闭。处理仪表故障后再次 Disable，必要时按该模块硬件操作说明人工退出输出。

### Measure 没有模块数据

检查 Run 前是否 Enable 模块。没有模块时程序会写一行温场/Monitor 系统状态并 Warning，不会中止。

### SEQ 旧 Measure/Initialize 无法运行

0.10.0 不兼容旧写法。删除 Initialize；把 Measure 改为无参数 `T Measure`，并在 Modules Manager Enable 相应测量方案。

### 左栏被长文件名撑宽

文件名标签会中间省略并在悬停时显示完整路径，不应再改变左栏最小宽度。若仍异常，请记录分辨率、UI Scale 和截图。

### 4K 字体不合适

使用 `ui_scale = "auto"`；也可在 `configs/default.toml` 设置 0.75–2.0 的手动倍率后重启。

### DAT 图不刷新

确认 Data Browser 打开的正是目标文件。它不会自动切换到当前 Run DAT；需要 View/Open/拖入一次。
