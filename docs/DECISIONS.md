# 架构决策记录

## ADR-001：使用 Python 和 PySide6

状态：Accepted

原因：

- 项目维护者熟悉 Python。
- 实验控制主要受 I/O、设备变化和等待稳定限制，不受 Python 计算速度限制。
- PySide6 能实现 Dock、MDI 子窗口、参数对话框和动态状态块。
- PyVISA、pyserial、厂商 SDK 包装和科学数据生态成熟。

限制：严格实时控制必须交给硬件、DAQ 或厂商驱动，不在 Windows GUI 进程内实现。

## ADR-002：GUI 和设备运行时分线程

状态：Accepted

Qt 主线程只处理界面；单独 asyncio 线程拥有设备实例。使用消息队列传递快照、事件和进度。

## ADR-003：TOML 配置

状态：Accepted

Python 3.11 内置只读 TOML 解析，减少运行依赖。配置可读、支持注释和设备数组。运行时不热重载以保证可追溯性。

## ADR-004：SEQ 文本 + AST

状态：Accepted

磁盘保持 MultiVu 风格单行文本，内存采用树结构支持任意嵌套。未知行原样保留。

## ADR-005：中央数值判稳

状态：Accepted

所有控制插件只提供读数和目标，中央算法统一使用误差、斜率和持续时间。设备原生状态以后可作为附加证据，但不能替代数值规则。

## ADR-006：活动事件锁存

状态：Accepted

Warning/Error 使用 source、code、context 去重。重复轮询只累计次数；恢复后解除；再次发生可重新提示。

## ADR-007：中止默认保持当前值

状态：Accepted

Stop 或 Error 不归零。默认调用插件 Hold，把目标锁定到中止瞬间当前值。可以通过配置改为 keep_target。

## ADR-008：数据浏览与测量输出解耦

状态：Accepted

Data Browser 不订阅 `DatRunLogger` 的当前文件，也不随新 Sequence 自动切换。用户打开或拖入哪个 DAT，就只监视该路径。这样可以分析历史数据、第三方数据或正在由其他程序追加的数据，且浏览操作不会改变实验记录位置。

文件变化采用修改时间与大小的 0.75 秒轮询。它对常见的追加、覆盖和原子替换都有效，并避免依赖不同 Windows 文件系统上行为不一致的单次文件通知。

## ADR-009：PLT 伴随文件和共享 X 模型

状态：Accepted

DAT 保持只读，显示状态存放在同目录同主文件名的 JSON `.plt`。规范名称是 `sample.plt`，同时接受 `sample.dat.plt` 作为导入兼容。格式包含版本和固定标识，引用列必须在当前 DAT 中全部存在后才应用。

同图多 Y 使用共同 Y 范围，便于比较相同量纲；纵向多图为每个 Y 保存独立范围，避免数量级差异压扁曲线。两种布局都只有一份 X 范围，因此纵向子图始终严格对齐。用户改变轴、布局或缩放后自动保存，以 DAT 文件为单位恢复分析视图。

## ADR-010：用 T/F 行前缀持久化 SEQ 启用状态

状态：Accepted

原始模板的每条启用行均以 `T ` 开头，因此沿用 MultiVu 风格用 `F ` 表示禁用，而不引入注释包装或额外元数据文件。解析后状态属于 Command 模型，复制、保存、重新加载和运行快照都能自然保留。

禁用容器只阻止执行进入其子树，不递归改写孩子的标志。这样重新启用 Scan 后可恢复孩子原先各自的 T/F 组合，也避免一次 Disable 造成大量不可逆编辑。界面另行计算有效启用状态，用灰色删除线表达祖先禁用。

## ADR-011：多行结构操作采用最外层选择

状态：Accepted

SEQ 显示列表允许 Ctrl/Shift 多选，但实际数据是树而不是平面行。Copy/Delete 先把显示选择映射为按文档顺序排列的命令，再移除已有选中祖先覆盖的后代；Scan 开始行和 End Scan 因共享命令 ID 只出现一次。这样无论用户以什么点击顺序选择，复制顺序都稳定，也不会在同时选择父 Scan 与子项时产生重复副本或重复删除。

Disable/Enable 采用不同语义：它们修改每个明确选中的命令自身，而不剪除后代。用户因此可以一次把父 Scan 及特定子命令都写成 `F`，并且重新启用父 Scan 不会意外启用仍应禁用的子项。

## ADR-012：Y 选择一次确认，X/Y 对数尺度全局独立

状态：Accepted

普通 QMenu 的可勾选动作在每次点击后自动关闭，不适合一次选择许多通道。因此右键只提供 `Select Y Series...` 入口，实际勾选放在保持打开的模态列表中；用户可修改任意多项后统一 OK，且至少保留一个 Y。选择结果按 DAT 列顺序保存，避免点击顺序导致不稳定布局。

X 与 Y 各保存一个全局尺度。X 尺度由 Overlay 和所有 Stacked 子图共享；Y 尺度统一应用于全部已选 Y，但 Stacked 仍保留各自数据范围。对数采用 Log10，只显示正值；非正值既不参与自动范围，也不参与点命中，曲线路径在无效点处断开。显示范围仍保存原始数据单位，使 PLT 可读并简化尺度切换验证。

PLT 写出版本升级为 2 并加入 `x_scale`、`y_scale`。读取版本 1 时使用 Linear 默认值，从而不破坏既有 DAT/PLT 文件对。

## ADR-013：辅助温度使用独立只读 Monitor 能力

状态：Accepted

`2nd Stage` 当前只用于观察，不能与主温度共享 `temperature` 类型。否则它会进入默认温度设备选择、中央判稳、中止 Hold 和温度 SEQ 的可控路径，容易被误当成标准温度。

因此新增通用 `monitor` 能力：插件只实现 Connect、Disconnect 和 Poll，快照只有 `current`；UI 显示 `Monitoring` 和只读说明，使用普通箭头光标且不发出双击控制信号。核心在目标校验入口再次拒绝 Monitor，形成界面与运行时双重保护。Monitor 可进入 Live Trend 作为临时显示，但默认不增加 DAT 数据列。

## ADR-014：轻量浅色主题与矢量工具栏图标

状态：Accepted

新版前端保留 Qt Fusion 基础样式，通过 Palette 和局部 QSS 建立浅色层级，并使用 QtAwesome 生成工具栏图标。关键设备值与运行状态局部放大，但全局字体固定为 10pt，避免菜单、命令栏和 SEQ 在普通分辨率下挤压内容。

未采用已加入但没有实际调用的 PyQtDarkTheme：无效果依赖会增加安装和打包复杂度，也使主题来源不清晰。长 SEQ 可能令 QListWidget 自动显示当前项右端，因此重建结束必须主动回到水平起点，确保命令类型和禁用标识始终可见。

## ADR-015：以原生分辨率驱动统一界面缩放

状态：Accepted

仅依赖固定 10pt 会令 4K 100% 缩放屏幕上的界面偏小，而只修改全局字体会使固定高度卡片、弹窗和绘图边距裁切。框架因此使用 Qt 的逻辑可用尺寸乘以设备像素比取得原生像素，统一缩放字体、窗口、卡片、图标及绘图几何。

自动倍率以 1920×1080 为 1.00×，对分辨率比取平方根并在 4K 封顶为 1.40×；这比按像素比直接放大更保守。观看距离、屏幕物理尺寸和用户视力无法仅由分辨率推断，因此配置允许 0.75× 到 2.00× 手动覆盖，且界面明确显示当前倍率。

## ADR-016：默认磁场原生单位采用 Oe

状态：Accepted

默认磁场设备、SEQ 新命令和 DAT 列统一采用 Oe。迁移时不仅修改标签，还把 ±9 T 范围、0.5/1 T/min 速率、判稳误差/斜率及仿真噪声按 10000 倍等比例换算，以保持物理行为和安全边界不变。

Oe 在状态、参数、SEQ 和 DAT 中写两位小数，K 温度写三位小数。解析器继续接受 T；T 命令写六位小数。参数窗口改变单位时同步换算所有磁场数值，防止单位选择与数值语义脱节。真实插件必须在厂商协议边界完成所需换算。

## ADR-017：显式温度列表保留顺序并整表预检

状态：Accepted

Scan Temperature 的 List 是用户声明的实验路径，不是待排序的数据集合。执行器因此保留原始顺序和重复项，不进行排序、去重或自动插值；回扫、热循环和在同一温度重复测量都能由一条单行命令准确表达。

列表解析由参数窗口、SEQ 解析/格式化和执行器共享。保存边界统一为 K 三位小数，执行边界再转换到设备原生单位。为避免列表后部的错误目标在前部动作完成后才被发现，执行器在首个目标下发前用当前设备配置预检全部点和统一速率；任一项失败即按 Error 路径中止并执行既有 Hold 策略。

## ADR-018：自定义数据路径采用逐命令授权

状态：Accepted

把 `allow_external_paths` 默认改为 true 会使来自其他电脑的旧 SEQ 可以在任意现存目录写文件，授权范围过宽；完全禁止外部路径又会让左侧文件选择器名义上可选、执行时却重定向。框架因此把用户明确选择的 Custom folder 保存为 `Set Datafile ... external <path>`，只授权该条命令的绝对目标。

未带 `external` 的命令继续服从全局配置并默认重定向；管理员仍可用 `allow_external_paths = true` 兼容受信任的旧流程。界面中的完整路径是数据，不应成为布局约束，因此文件标签采用中间省略和 Tooltip，并使用忽略水平 size hint 的策略。

## ADR-019：SEQ 参数限制与设备配置同源

状态：Accepted

运行器原本会在设备动作前检查 `min_value`、`max_value` 和 `max_rate_per_minute`，但 SEQ 参数弹窗使用通用大范围，用户只能在运行时才发现越界。手动控制与 SEQ 因此出现两套不一致的输入边界。

现在主窗口把当前 `DeviceConfig` 集合传入 SEQ 参数窗口，窗口按 `device_id` 和设备类型选取限制。Set/Linear Scan 使用数值控件范围，Temperature List 在确认时逐点检查；磁场范围随 Oe/T 选择转换。界面校验只用于尽早反馈，不能取代执行器复检，这样手写 SEQ、旧文件和绕过 GUI 的调用仍受到同一安全边界保护。

## ADR-020：测量方案与控制设备彻底分离

状态：Accepted

删除 `measurement` DeviceKind、旧 Transport 状态块和 DevicePlugin.measure。温度、磁场、Monitor 继续使用设备插件；任何测量仪表组合都使用 Measurement Module。完整测量方案往往同时拥有源表、表桥、切换器及内部时序，无法自然映射为底部单设备状态块。旧 `[[devices]] kind="measurement"` 不兼容，模块也不能通过 API 控制温场。

## ADR-021：Frontend 主进程、Backend 每模块独立进程

状态：Accepted

模块自定义 PySide6 UI 在 GUI 线程运行；每个 Enabled backend 使用独立 spawn 进程，仪表 I/O 只在该进程发生，通过串行 IPC 通信。这样满足 Qt 线程规则，并隔离阻塞通信、仪表状态和部分崩溃影响。模块返回值必须可序列化；进程不是恶意源码安全沙箱。

## ADR-022：模块生命周期区分 End 与 Abort

状态：Accepted

每次 Run 调用 begin_sequence 和 end_sequence(completed|stopped|error)；abort 只用于 Disable 和应用退出。SEQ Error 不调用 abort，因为模块仍应保持连接供用户检查。end 失败使运行 Faulted 但模块保持 Enabled；abort 失败使 Disable 失败，不能隐藏窗口或伪造 Disabled。

## ADR-023：无参数 Measure 与 Run 级模块锁定

状态：Accepted

SEQ 只接受 `T Measure`。模块选择在 Run 前通过 Enabled 状态完成，一条 Measure 并行调用全部锁定模块并等待全部完成。嵌套 Scan 已能表达循环与间隔；Run 级锁定保证 Schema 和生命周期一致。旧 Measure 参数产生 Error；无模块时 Warning + 系统快照而不是中止。

## ADR-024：模块流式多行、中央唯一写 DAT

状态：Accepted

模块声明固定列并通过 emit_row 流式发值。中央为每行捕获最新系统快照、自动加模块 ID 前缀并立即 Flush；模块不能直接写实验 DAT。这样 R1–R4 顺序测量可拥有不同温场，并避免多个进程并发写同一文件。未声明列/复杂值为 Error，模块自行声明 Status/Warning 列。

## ADR-025：Settings desired 与实际 Status 分开保存

状态：Accepted

Enable 只加载 Settings 不 Apply；显式 Apply 才发送。Run 分别保存 `<id>.settings.toml` 和 `<id>.status-at-start.json`，因为界面期望值不等于仪表当前状态。未 Apply 修改在 Run 前必须选择 Apply and Run、Run Without Applying 或 Cancel。

## ADR-026：所有模块共享依赖目录

状态：Accepted

模块依赖统一安装到 `module_runtime/site-packages`；离线 wheels 优先，在线安装二次确认；版本范围冲突禁止 Enable。逐模块 venv 会增加包体和启动开销，实验室应协调少量仪表库版本。发布 EXE 安装新依赖时需配置便携 Python，但运行时仍从共享 target 目录加载。

## ADR-027：模块窗口不可由用户直接关闭

状态：Accepted

模块窗口是主窗口拥有的独立 modeless 窗口，可移动/最小化但移除关闭能力；只有 Disable 成功才隐藏。窗口可见性因此准确反映 Enabled 会话，关闭窗口不会被误解为输出已停止。主窗口最小化会联动最小化当前可见模块窗口。
