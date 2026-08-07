# 操作手册

## 安装与启动

### Windows 发布包

解压整个 `OpenLabControl` 文件夹后运行：

```text
OpenLabControl.exe
```

不要只复制 EXE；`configs/`、`docs/`、`plugin_templates/`、`modules/`、
`device_plugins/`、`plugin_runtime/`、`plugin_state/`、`module_data/`、`wheels/` 和
`runs/` 等目录应和它一起保留。首次启动所有测量模块都是 Disabled，这是固定安全行为，
不会恢复上次 Enable 状态。

### 源码运行

```text
setup.bat
run.bat
```

`setup.bat` 默认安装 `requirements-lock.txt` 中经过发布验证的精确版本。需要评估依赖
升级时，同时修改 `requirements.txt`、`pyproject.toml` 和核心共享依赖表，在隔离环境
更新锁定文件，再完成全部测试和打包验收；不要在发布环境中临时升级单个包。

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

这个参数只服务于自动验证；正常 GUI 启动不会恢复 Enabled 状态，仍需用户在 Modules
Manager 手动启用。无界面模式不会弹信任确认：模块必须已经手动复制、在 GUI 中确认过
完全相同的内容指纹；存在额外依赖时，其离线 runtime 也必须已准备好，否则启动会拒绝。

## 报警报告

1. 把 `integrations/nonebot_alarm_receiver/` 复制到 NoneBot2 插件目录。
2. 在 NoneBot 环境文件配置随机 `alarm_token`、`alarm_admin_qqs` 和
   `alarm_tester_qqs`；管理员与测试员列表可重叠，接收器会去重。
3. 在运行 OpenLab Control 的账户环境中设置同一 Token，例如
   `OPENLAB_ALARM_TOKEN`；或者创建独立 Token 文件并配置 `token_file`。
4. 在 `configs/default.toml` 的 `[alarms.reporting]` 中核对地址后设置
   `enabled = true`，重启程序。
5. 先用仿真 `Inject Warning`、`Inject Error` 验证：Warning 只到测试员，
   Error 同时到管理员和测试员，同一活动事件不会反复推送。

接收端不再接受 `target_qq`，避免持有 Token 的发射端任意选择收件人。远程接收器必须
使用 HTTPS。报警网络故障只产生本地 Warning，不会替代仪表互锁，也不会改变 Error
中止 SEQ、Stop 后温场 Hold Current 的行为。

## 主窗口

- 左侧 `Sequence Control`：数据文件、当前 SEQ、运行状态和 Run/Pause/Stop。
- 中央：浮动 SEQ 和 Data Browser 窗口。
- 右侧 `Sequence Command Bar`：双击命令后设置参数并插入。
- 底部 `Device Status`：Temperature、Magnetic Field、`2nd Stage` 等控制/Monitor；不再显示测量 Transport 块。
- 工具栏 `Modules`：测量模块管理。
- `Run Log`：Warning、Error、步骤和模块手动动作记录，可从 View 菜单显示。
- `Live Trend`：保留最近设备快照，最多每 250 ms 合并一次可见重绘；只影响显示。

温度显示三位小数；Oe 显示两位。温度/磁场状态块双击打开手动控制，Monitor 只显示，
不弹控制窗口。每种 temperature/field 最多一个 primary 供 SEQ 使用；其他 secondary
默认只读显示。即使 secondary 显式允许手动控制，SEQ 也不会自动选择它。

## 测量模块

### Enable

1. 点击工具栏或菜单 `Modules`。
2. 管理器只有 `Enabled / Name / Version` 三列。
3. 勾选所需模块。
4. 首次加载或内容变化时，核对弹窗中的类型、ID、版本、完整路径和 SHA-256 指纹后确认
   信任；拒绝时不加载任何源码。
5. 程序显示 `Initializing <module>...`；初始化成功后才真正勾选并打开模块窗口。
6. 初始化失败会弹 Error，仍保持 Disabled。

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

### SEQ 对应的模块设置

File → Save 同时保存 `<SEQ 文件名去掉 .seq>.modules.toml`。文件包含当前 SEQ 已导入的
模块设置，以及保存时所有 Enabled 模块的 Settings 页当前值。File → Open 会自动导入
同名伴随文件；旧 SEQ 没有该文件时继续使用 `module_data/<id>/settings.toml`。

导入不是 Apply：

- 启动默认 Disabled 的规则不变，Load 不会替用户勾选模块；
- 已 Enabled 模块只更新界面并标为未 Apply；
- 下一次 Run 会按既有流程询问 Apply and Run / Run Without Applying / Cancel；
- 模块缺失或版本不同会显示 Warning；真实仪表参数必须重新核对；
- Save As 会为新 SEQ 生成新的同名伴随文件，复制实验时应同时复制这两个文件。

打开运行目录中的 `sequence.seq` 时，程序还可直接读取该目录已经保存的
`module_settings/*.settings.toml`，方便从历史运行快照恢复实验参数。

### Run 前未 Apply

如果 Settings 页面有修改但尚未 Apply，Run 会询问：

- `Apply and Run`：先等待所有修改成功 Apply，再启动。
- `Run Without Applying`：仪表保持现状，但把当前界面值作为 desired settings 保存到运行目录。
- `Cancel`：不运行。

### Disable

取消勾选时：

1. 先保存 Settings；
2. 调用模块 `close(api)`；
3. close 完成后取消勾选、关闭工作进程并隐藏窗口。

close 失败时程序报告 Error，并在关闭总上限内强制回收模块工作进程、最终显示 Disabled。
强制回收只保证本机进程/管道不残留，不代表外部仪表已经安全；此时必须按模块硬件说明
人工检查输出。

### Refresh 与依赖

只有 SEQ Idle 且全部模块 Disabled 时可以 `Refresh`。它重新扫描 `modules/`，不做运行中的热替换。

框架共享依赖不需要安装。只有所选模块声明框架尚未提供的额外依赖时，Manager 才显示
`Install Dependencies`；安装只要求该模块 Disabled，其他模块的隔离 runtime 不会被
替换。Refresh 仍要求全部模块 Disabled，因为它会重新建立整个发现列表。

额外依赖缺失时：

1. Disable 目标模块，再选中它。
2. 点击 `Install Dependencies`。
3. 核对并确认该模块的内容指纹。
4. 程序只读取模块 `requirements.lock`、根 `wheels/` 和模块 `wheels/`，使用精确版本
   和 SHA-256 离线安装到该模块自己的 runtime。
5. 完成后 Refresh。

没有在线回退。PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 是明确
由主框架提供的统一版本，不属于“借用”；其他额外包只能来自已验证的隔离 runtime。

## 编辑 SEQ

### 新建/打开/保存

- File → New/Open/Save/Save As。
- 关闭浮动 SEQ 后，点击 New/Open/Edit 会重新显示现有编辑器。
- 文件扩展名是 `.seq`。
- 有模块设置的实验还会保存同名 `.modules.toml`；Load 时自动导入但不 Apply。

### 插入与修改

- 右侧命令双击：弹参数窗口，确认后插入。
- SEQ 行双击：编辑该行参数。
- `Set Datafile` 的 `Browse…` 直接打开 Windows 文件窗口；`open` 选已有文件，
  `create` / `open|create` 可选或新建文件。
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
- 第一条指令前发送模块 `run_start` 事件并冻结 `slots`；
- 每个 Measure 按逻辑槽位并行调用参与模块的 `measure(slot, api)`；
- 最终按 completed/stopped/error 发送每个模块的 `run_end` 事件。

### Pause

Pause 在安全检查点暂停 SEQ 调度；不会主动关闭模块输出，也不会断开设备。Resume 继续。

### Stop

Stop 后：

- 温度和磁场执行 Hold Current；
- 模块收到 `run_end`，reason 为 `stopped`；
- 不执行模块 close，模块仍 Enabled，窗口保持可用。

如果主温度或磁场设备读链路中断，状态显示 `Reconnecting`。默认每 2 秒重连，最长
1 分钟；SEQ 在这段时间冻结当前 Wait/Settle 计时。成功后核对仪表实际 target/rate 再
继续；超时或核对失败进入 Error。写操作超时不会自动重发。

## 手动温场控制

双击底部 Temperature 或 Magnetic Field：

- Target；
- Rate；
- Settle/Sweep；
- `Set`；
- `Hold Current`。

弹窗使用配置上下限和最大速率。`2nd Stage` 等 Monitor 没有手动控制。
`control_enabled = false` 的 secondary 也没有手动控制。运行时仍会再次检查角色、
连接状态、Target 和 Rate，因此不能通过脚本绕过参数窗口限制。

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
- 模块收到 reason 为 `error` 的 `run_end`，不调用 close；
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

`Timestamp(s)`、`Time Stamp (sec)` 等绝对时间列在线性坐标下会显示实际日期时间。
Quantum Design 文件优先按 `FILEOPENTIME` 校准；OpenLab 文件按
`TIMESTAMP_EPOCH` 和 `Started` 换算。若文件没有足够证据确认 epoch，则保留原始
秒值，避免显示一个看似精确但错误的日期。双击详情的摘要显示完整实际时间，下面的
原始字段仍显示文件内秒值。

普通线性坐标采用 `1/2/5 × 10ⁿ` 主刻度；时间坐标按整毫秒、秒、分钟、小时或日期
对齐。缩放后会重新选择适合当前范围的刻度，不会改变数据本身。

## 输出与备份

一次 Run 至少保留：

```text
sequence.seq
configuration.toml
module_settings/*.settings.toml
module_settings/*.status-at-start.json
experiment.dat
device_status.dat
events.dat
```

建议实验结束后整体复制整个运行目录，而不是只复制 DAT。模块 desired 设置和实际 Status 对复现实验同样重要。
Load 运行目录中的 `sequence.seq` 会兼容导入这些 desired settings，但不会自动发送到
仪表。

`device_status.dat` 默认每秒保存温度、磁场和 Monitor 的当前值、目标、速率、动作、
稳定性、连接状态和读数年龄。它只在 Run 期间写入，且不会因打开 Live Trend 而改变
记录频率或增加仪表查询。

## 关闭程序

SEQ 运行中关闭主窗口会确认；确认后请求 Stop/Hold/End。随后：

1. 保存所有 Enabled 模块 Settings；
2. 对每个 Enabled 模块调用 `close(api)`；
3. 断开温场/Monitor；
4. 关闭日志与应用。

下次启动所有模块仍是 Disabled，但 Settings 值会在下次 Enable 时自动载入且不自动 Apply。

## 常见问题

### 模块无法勾选

选中行查看底部说明。常见原因：清单 Invalid、未确认信任、
框架共享依赖范围不兼容、额外依赖缺失、lock/wheel 哈希错误或隔离 runtime 被修改。
Disable 目标模块后重新准备额外依赖；需要重新扫描源码时先 Disable 全部模块再
Refresh。

### 发布包提示没有 Python Runtime

只有扩展声明额外依赖时才会显示 Install Dependencies，并需要配置
`modules.python_executable` / `plugins.python_executable`，或放置
`runtime/python/python.exe`。框架共享依赖（包括 PyVISA）已经在 EXE 内，不需要便携
Python；只有离线准备额外依赖需要，且便携 Python 必须自带 pip。

### Enable 后参数没有作用

这是设计行为。Enable 只初始化并加载保存值，必须点击 `Apply Settings` 并确认才发送。

### 模块窗口关不掉

这是安全设计。先在 Modules Manager 取消 Enable；close 完成后窗口自动隐藏。

### Disable 报错但已显示 Disabled

close 没有确认完成，但框架已为资源释放强制关闭工作进程。不要把 Disabled 当作输出已
关闭；查看 Error，并按该模块硬件操作说明人工退出输出、检查仪表状态后再重新 Enable。

### Measure 没有模块数据

检查 Run 前是否 Enable 模块。没有模块时程序会写一行温场/Monitor 系统状态并 Warning，不会中止。

### SEQ 旧 Measure/Initialize 无法运行

当前版本不兼容旧写法。删除 Initialize；把 Measure 改为无参数 `T Measure`，并在
Modules Manager Enable 相应测量方案。

### 更换温控仪或磁体电源

取得并审查与目标设备匹配的 Device Plugin 后，把完整插件目录复制到
`device_plugins/`，在一个配置文件中修改对应设备的 `plugin = "<plugin-id>"`、
address、上下限、速率和超时，然后重启。不要为不同仪表维护核心代码分支。首次启动
会要求确认插件内容指纹；修改插件后必须重新确认。核心自带内容只作为实现示例。

### 设备一直 Reconnecting

不要在恢复窗内反复点击 Set/Hold。检查物理链路和仪表面板；程序会自动重建独立设备
进程并读取实际状态。1 分钟仍失败时会 Fault，不会无限重试或猜测安全状态。

### 左栏被长文件名撑宽

文件名标签会中间省略并在悬停时显示完整路径，不应再改变左栏最小宽度。若仍异常，请记录分辨率、UI Scale 和截图。

### 4K 字体不合适

使用 `ui_scale = "auto"`；也可在 `configs/default.toml` 设置 0.75–2.0 的手动倍率后重启。

### DAT 图不刷新

确认 Data Browser 打开的正是目标文件。它不会自动切换到当前 Run DAT；需要 View/Open/拖入一次。
