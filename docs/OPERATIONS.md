# 操作手册

## 日常运行的最短流程

1. 启动后检查已配置的 System 面板和实际状态；全新安装没有面板也是有效状态。
2. 打开 SEQ；需要测量模块时再 Enable，并核对 Status 和 Settings。
3. 有未 Apply 的设置时优先选择 **Apply and Run**；真机首跑不要跳过 Apply。
4. 核对数据文件位置，点击 Run；结束后检查 DAT、`events.dat` 和 `instrument_status.dat`。
5. 分享运行目录前检查 `configuration/`，其中可能含真实仪表地址、本机路径和现场 PID。

下面各节再解释安装、模块管理、异常和维护操作。

## 安装与启动

### Windows 发布包

解压整个 `OpenLabControl` 文件夹后运行：

```text
OpenLabControl.exe
```

不要只复制 EXE；`configs/`、`docs/`、`modules/`、`system_instruments/`、`module_data/` 和
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

指定 SEQ：

```text
.venv\Scripts\python.exe run.py --sequence examples\nested_scan.seq
```

程序读取唯一通用配置 `configs/general.toml`。System Instrument 实例与现场限制由
Instrument Scanner 写入 `configs/instruments/`；未分配 VISA 写入
`configs/visa.resources.toml`。全新安装没有 System 面板也能启动，三个内置仿真默认关闭。

单独打开 DAT Browser：

```text
.venv\Scripts\python.exe run.py --data-file C:\Data\sample.dat
```

无界面验证模块运行时，可显式重复使用 `--enable-module ID`：

```text
.venv\Scripts\python.exe run.py --headless-demo --enable-module simulated_transport --sequence examples\module_measurement.seq --timeout 30
```

这个参数只服务于自动验证；正常 GUI 启动不会恢复 Enabled 状态，仍需用户在 Modules
Manager 手动启用。

## 报警报告

1. 在独立 NoneBot 接收服务中提供 `/alarm/report`，校验 `X-Token`，并按报警等级选择收件人。
2. 在 NoneBot 环境文件配置随机 `alarm_token`、`alarm_admin_qqs` 和
   `alarm_tester_qqs`；管理员与测试员列表可重叠，接收器会去重。
3. 在运行 OpenLab Control 的账户环境中设置同一 Token，例如
   `OPENLAB_ALARM_TOKEN`；或者创建独立 Token 文件并配置 `token_file`。
4. 在 `configs/general.toml` 的 `[alarms.reporting]` 中核对地址后设置
   `enabled = true`，重启程序。
5. 先用仿真 `Inject Warning`、`Inject Error` 验证：Warning 只到测试员，
   Error 同时到管理员和测试员，同一活动事件不会反复推送。

接收端不再接受 `target_qq`，避免持有 Token 的发射端任意选择收件人。远程接收器必须
使用 HTTPS。报警网络故障只产生本地 Warning，不会替代仪表互锁，也不会改变 Error
中止 SEQ 或“SEQ 退出不控制 System Instrument”的行为。

## 主窗口

- 左侧 `Sequence Control`：数据文件、当前 SEQ、运行状态、Enabled 模块监视卡和
  Run/Pause/Stop。
- 中央：浮动 SEQ 和可同时打开多个文件的 Data Browser 窗口。
- 右侧 `Sequence Command Bar`：双击命令后设置参数并插入。
- 底部无外层标题的仪表面板条：按扫描器保存的全局顺序显示所有已启用固定面板；没有启用面板
  时保持为空；
  `controller` 保持当前值、目标、速率和稳定状态样式，`readout` 只显示一个主读数，
  `readout_grid` 以 2×2 最多显示四个读数；需要更多读数时由作者声明另一个固定面板。
  `switch` 显示 0/1 状态和简单系统指令按钮。面板使用
  固定浅色配色，不跟随 Windows 深色主题变色；
  不再显示测量 Transport 块。
- 工具栏 `Modules`：测量模块管理。
- `Run Log`：Warning、Error、步骤和模块手动动作记录，可从 View 菜单显示。
- `Live Trend`：保留最近仪表快照，最多每 250 ms 合并一次可见重绘；只影响显示。

### 外观、字号与窗口大小

打开 **View → Appearance**：

- `Overall size` 同时调整按钮、图标、间距、窗口最低尺寸和文字，范围 75%–200%；
- `Text size` 在整体缩放上再单独调整文字，范围 70%–150%；
- `At startup` 可选择记住上次窗口尺寸与位置、始终最大化或始终使用默认布局；
- `Reset Window Positions` 会在保存后清除主窗口、SEQ、Data Browser、Live Trend、
  手动控制和 Measurement Module 窗口的旧位置。

保存后重启生效。程序不会在运行中重建模块窗口，因此不会因为改字号产生重复信号、重复
worker 或短暂仪表连接。个人外观值保存在操作系统用户配置目录，不进入主 TOML、SEQ、
DAT、模块设置或运行快照。源码版和 Windows 打包版使用相同规则。

若尚未保存过 Appearance，主配置的 `application.ui_scale` 仍是整体缩放默认值；在对话框
中保存后，个人选择优先。点击 `Restore Defaults` 会恢复主配置默认缩放、100% 文字和窗口
记忆模式，并清除旧窗口位置。

每个读数的小数位来自对应 System Instrument 清单。`controller` 面板双击打开手动控制；
`readout` 和 `readout_grid` 不弹控制窗口。`sample_temp` 与 `field` 角色各自全局最多一个，
供标准温场 SEQ 使用；角色为 `none` 的 controller 仍可手动控制，但不会被标准温场 SEQ
选择。

## 测量模块

### Enable

1. 点击工具栏或菜单 `Modules`。
2. 管理器只有 `Enabled / Name / Version` 三列。
3. 勾选所需模块。
4. 程序直接加载所选目录中的模块代码，并显示 `Initializing <module>...`；初始化成功后才
   真正勾选并打开模块窗口。
5. 初始化失败会弹 Error，仍保持 Disabled。

程序不维护模块信任状态，也不在 Enable 时弹出代码指纹确认。只把来源已经核对过的模块
复制到 `modules/`；第三方模块是可执行代码，安装前应由操作者在框架外确认来源和内容。

如果模块声明了自己的 SEQ 指令，完成第 5 步后，右侧 `Sequence Command Bar` 会新增一个
直接以模块显示名称命名的顶层组。模块尚未 Enable 时不预先显示；Disable、初始化失败或
worker 通信失效后立即移除，不会留下一个无法执行的菜单项。

模块窗口是独立浮动 Windows 窗口：可移动、最小化，保持在主窗口之前但不全局置顶；
用户不能用关闭按钮/Alt+F4 关闭。双击管理器中的 Enabled 模块，或点击主窗口左侧的
模块卡片，都可把窗口带到前面。

每个 Enabled 模块在 `Sequence Status` 下方有一张紧凑卡片。它显示 Enabled/Measuring、
最小化状态和活动 Warning/Error；模块若选择了结果列，还会显示最近通道值。新一轮
`Measure` 会先清空旧值，空值显示 `—`。这些内容来自已经返回的测量结果，不会额外访问
仪表。模块较多时只出现纵向滚动条，Run/Pause/Stop 始终留在左栏底部。

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

`Run Without Applying` 是高级选项：只有已经人工读回并确认仪表实际状态，而且理解快照中
desired settings 可能与实际状态不同时才使用。真机首跑或状态不确定时选择 Cancel，核对后
再 Apply；不要为了省去确认直接跳过。

### Disable

取消勾选时：

1. 先保存 Settings；
2. 调用模块 `close(api)`；
3. close 完成后取消勾选、关闭工作进程并隐藏窗口。

close 失败时程序报告 Error，并在关闭总上限内强制回收模块工作进程、最终显示 Disabled。
强制回收只保证本机进程/管道不残留，不代表外部仪表已经安全；此时必须按模块硬件说明
人工检查输出。

### Refresh

只有 SEQ Idle 且全部模块 Disabled 时可以 `Refresh`。它重新扫描 `modules/`，不做运行中的热替换。

Refresh 仍要求全部模块 Disabled，因为它会重新建立整个发现列表。所有模块使用主框架
锁定的同一组依赖，界面不提供单模块依赖安装；需要新 Python 包时必须更新核心依赖、
重新测试并构建发布包。

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
- `Scan Field` 可选 `Choose nearer start or its negative`。它在该命令实际执行时取得已通过
  新鲜度检查的当前场读回，选择输入路径或整条反号路径；选择结果写入 Run Log。开关默认关闭。

### 模块自定义指令

模块完成 Enable 后，可在右侧直接出现例如：

```text
Keithley 2400
├─ Set Current
└─ Scan Current
```

- `Set Current` 一类普通指令完成模块动作后继续下一行，不产生 DAT 行。
- `Scan Current` 一类扫描指令与温场 Scan 一样可任意嵌套；每个点先由该模块设置成功，
  再执行该点下面的全部子命令。
- 子命令是 `Measure` 时，仍按当前所有 Enabled 模块和逻辑槽位规则测量；模块扫描不会
  暗中替用户增加一次测量。
- 同一模块的自定义指令、`measure`、`run_start/run_end` 共用一个串行 worker，不会
  同时访问同一 VISA/GPIB session；不同模块在 `Measure` 中仍可并行。
- 自定义指令对仪表的改变只属于本次运行状态，不修改 Settings 页或持久设置。

SEQ 使用通用 `Module Command/Module Scan` 文本，因此即使相关模块未安装也不会丢失
原始行。此时行显示红色；Load 只给 Warning，Run 则要求对应模块已 Enable 且仍声明同一
稳定指令 ID。框架不会自动 Enable、Apply、安装模块或把未知指令当成其他指令执行。

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

1. 检查全部已启用 System 面板；含温场命令时确认 `sample_temp` / `field` 面板已连接。
2. Enable 所需模块并检查 Status。
3. 确认需 Apply 的 Settings 已发送。
4. 保存或核对 SEQ；Run 会另外保存实际执行快照。
5. 确认数据文件位置和磁盘空间。
6. 点击 Run。

Run 开始后：

- SEQ、模块 Enable/Disable/Refresh、Settings/Apply 和手动动作锁定；
- 所有 Enabled 模块成为本次固定 Schema；
- 第一条指令前发送模块 `run_start` 事件并冻结 `slots`；
- 模块自定义指令在同一冻结模块集合和同一串行 IPC 中执行；
- 每个 Measure 按逻辑槽位并行调用参与模块的 `measure(slot, api)`；
- 最终按 completed/stopped/error 发送每个模块的 `run_end` 事件。

### Pause

Pause 在安全检查点暂停 SEQ 调度；不会主动关闭模块输出，也不会断开仪表。模块自定义
长循环须调用 `api.checkpoint()` 或 `api.sleep()` 才能在循环中响应；已经进入厂商驱动的
阻塞调用只能等待其有限 I/O timeout。Resume 从原位置继续。

### Stop

Stop 后：

- 温度和磁场保持仪表原有目标、速率和动作，框架不发送 Set 或 Hold；
- 模块收到 `run_end`，reason 为 `stopped`；
- 不执行模块 close，模块仍 Enabled，窗口保持可用。

如果主温度或磁场仪表读链路中断，状态显示 `Reconnecting`。默认每 2 秒重连，最长
1 分钟；SEQ 在这段时间冻结当前 Wait/Settle 计时。成功后核对仪表实际 target/rate 再
继续；超时或核对失败进入 Error。写操作超时不会自动重发。

## 手动温场控制

双击底部 Temperature 或 Magnetic Field：

- Target；
- Rate；
- Settle/Sweep；
- `Set`；
- `Hold Current`。

弹窗使用对应 controller 面板的配置上下限和最大速率。`2nd Stage` 等只读面板没有手动
控制。角色为 `none` 的 controller 仍可从自己的面板手动控制，但标准温场 SEQ 不会选择
它。运行时仍会再次检查控制端点、连接状态、Target 和 Rate，因此不能通过脚本绕过参数
窗口限制。

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
- 未注册特殊响应时，不向 System Instrument 发送控制指令；
- 模块收到 reason 为 `error` 的 `run_end`，不调用 close；
- 已写数据保留。

典型：仪表掉线、互锁、二级冷头过温、源表硬件报警、模块 Schema 违规。

如果同一故障被轮询/多次测量反复报告，只会更新计数，不会连续弹窗轰炸。恢复后事件 RESOLVED；再次发生才重新弹。

System Instrument 可以为自己的稳定事件代码注册响应。命中后核心会独立执行该动作；例如
`zero` 会按目标磁场的默认速率把目标设为零。同一活动事件只执行一次，并锁定目标，直到
源事件解除并人工复位。当前正式 System Instrument 没有注册任何响应，因此默认不会发生
跨仪表联动。

## Data Browser

每个 Data Browser 窗口独立保存自己的 DAT：

1. 左侧数据区的 View 会新建窗口并直接载入当前 Run DAT。
2. Graph → Data Browser 新建空白窗口，随后可打开任意 DAT；把 `.dat` 拖入主窗口也会
   新建窗口。
3. 多个窗口可以同时显示不同数据；文件追加后各自自动刷新图。

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
configuration/
module_settings/*.settings.toml
module_settings/*.status-at-start.json
experiment.dat
instrument_status.dat
events.dat
```

建议实验结束后整体复制整个运行目录，而不是只复制 DAT。模块 desired 设置和实际 Status 对复现实验同样重要。
Load 运行目录中的 `sequence.seq` 会导入这些 desired settings，但不会自动发送到
仪表。

运行目录中的 `configuration/` 按原结构保存本次使用的 `general.toml`、存在时的
`visa.resources.toml`、`instruments/*.toml` 和 `pid/*.toml`。它可能含真实仪表地址、本机
路径和现场 PID。内部备份可以整体保留；对外分享或提交 Git 前必须检查并脱敏。

`instrument_status.dat` 默认每秒保存温度、磁场和 Monitor 的当前值、目标、速率、动作、
稳定性、连接状态和读数年龄。它只在 Run 期间写入，且不会因打开 Live Trend 而改变
记录频率或增加仪表查询。

## 关闭程序

SEQ 运行中关闭主窗口会确认；确认后请求 Stop/End。随后：

1. 保存所有 Enabled 模块 Settings；
2. 对每个 Enabled 模块调用 `close(api)`；
3. 断开温场/Monitor；
4. 关闭日志与应用。

下次启动所有模块仍是 Disabled，但 Settings 值会在下次 Enable 时自动载入且不自动 Apply。

## 常见问题

### 模块无法勾选

选中行查看底部说明。常见原因是 `module.toml`/`backend.py` 缺失或清单字段无效。修正后
先 Disable 全部模块，再 Refresh。缺少 Python 包属于发布包依赖问题，不能在模块管理器中
临时安装。

### Enable 后参数没有作用

这是设计行为。Enable 只初始化并加载保存值，必须点击 `Apply Settings` 并确认才发送。

### 模块窗口关不掉

这是安全设计。先在 Modules Manager 取消 Enable；close 完成后窗口自动隐藏。

### Disable 报错但已显示 Disabled

close 没有确认完成，但框架已为资源释放强制关闭工作进程。不要把 Disabled 当作输出已
关闭；查看 Error，并按该模块硬件操作说明人工退出输出、检查仪表状态后再重新 Enable。

### Measure 没有模块数据

检查 Run 前是否 Enable 模块。没有模块时程序会写一行温场/Monitor 系统状态并 Warning，不会中止。

### Measure 没有参数怎样选择模块

当前 SEQ 使用无参数 `T Measure`。要参与测量的方案在 Modules Manager 中 Enable；Run
开始时程序冻结模块集合、列和槽位。

### 更换温控仪或磁体电源

取得并审查与目标仪表匹配的 System Instrument 后，把完整目录复制到
`system_instruments/`，再打开 Instrument Scanner。在该型号页面添加物理实例、选择 VISA
地址或填写模板声明的专用连接字段，并确认固定面板、角色、读数、上下限、速率和稳定参数。
最后一页检查全部面板的顺序和完整写入预览后保存，再重启。分配给 System Instrument 的
VISA 地址不会保留在 Measurement 资源清单；核心自带内容只作为实现示例。

### 仪表一直 Reconnecting

不要在恢复窗内反复点击 Set/Hold。检查物理链路和仪表面板；程序会自动重建独立仪表
进程并读取实际状态。1 分钟仍失败时会 Fault，不会无限重试或猜测安全状态。

### 左栏被长文件名撑宽

文件名标签会中间省略并在悬停时显示完整路径，不应再改变左栏最小宽度。若仍异常，请记录分辨率、UI Scale 和截图。

### 4K 字体不合适

先使用 **View → Appearance**：整体大小保持 `Automatic`，再单独选择 70%–150% 的
`Text size`。如果个人外观文件尚未建立，也可以在 `configs/general.toml` 中用
`ui_scale = "auto"` 或 0.75–2.0 设定现场默认值后重启。

多显示器更换后若窗口落在不可见区域，选择 `Reset Window Positions` 并保存；下次启动会
使用当前屏幕的默认布局。

### DAT 图不刷新

确认 Data Browser 窗口标题对应目标文件。查看当前 Run 时点击左侧数据区的 **View**；
查看其他文件时使用 **Graph → Data Browser → Open DAT**，或把 DAT 拖入主窗口。
