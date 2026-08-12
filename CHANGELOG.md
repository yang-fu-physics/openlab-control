# Changelog

## Unreleased

## 0.15.2 - 2026-08-12

- 正式 Windows EXE 和 ZIP 只由 GitHub Actions 在干净的 Windows runner 中构建、测试并
  上传；本地不再生成或上传发布资产，避免本机环境与正式包不一致。
- 修正 GitHub Windows runner 同时使用用户目录长路径与 8.3 短路径时的测试比较；两种
  写法现在按同一真实目录处理，不再阻止云端发布。
- 发布包根目录同时提供 `OpenLabControl.exe` 与 `InstrumentScanner.exe`，并共享唯一的
  `_internal`；不包含 `tools/` 目录，也不重复打包 Python、PySide6 和 PyVISA 运行时。
- 本版本包含 0.15.0/0.15.1 的 Instrument 资源架构和只读扫描器变更。真实仪表仍未完成
  现场验证，首次接入必须保留仪表自身的硬件限值与安全状态。

## 0.15.1 - 2026-08-12

- Windows 发布包把 `OpenLabControl.exe` 与 `InstrumentScanner.exe` 放在根目录；两个 onedir
  程序共享唯一的 `_internal`，不再为扫描器重复打包 PySide6、PyVISA 和 Python 运行时。
- 发布包不再创建 `tools/`。源码仍保留 `tools/instrument_scanner.py`，继续复用主项目
  `.venv`；打包版的两个 EXE 都必须与 `_internal` 一起保留，不能只复制扫描器 EXE。
- 新增 Windows Stable Release GitHub Actions：标签与项目版本一致时，在 GitHub Windows
  Runner 上执行完整核心测试、严格文档构建、语法编译、双 EXE 打包、解压内容检查和
  冒烟测试，再由 GitHub 内部网络创建稳定 Release 并上传 ZIP 与 SHA-256。
- 本版本包含 `v0.15.0` 源码标签引入的不兼容 Instrument 资源架构、System Instrument
  API 1.2、统一 Measurement 资源选择和只读仪表扫描器。真实仪表仍未完成现场验证。

## 0.15.0 - 2026-08-12

- 采用不兼容的新命名：核心源码、配置、SEQ 参数、DAT 状态文件和公共 API 全部统一使用
  `instrument`。`labcontrol.devices`、`Device*` 类型、`device.toml`、`device_plugins/`、
  `[plugins]` 与 `plugin = ...` 均已删除，不提供旧名称映射。
- System Instrument 使用 `system_instruments/<id>/instrument.toml`、`[[instruments]]` 和
  `backend`；Measurement Module 继续使用独立的 `modules/<id>/module.toml` 与按需 Enable
  生命周期。两者不再共用旧的插件/扩展术语。
- 共用的哈希信任、离线额外依赖和受控 Python 加载代码移到中性的 `package_support`，
  但不会合并 System Instrument 与 Measurement Module 的接口、清单或生命周期。
- 重写 System Instrument 开发网站，增加第一个仪表、读取与状态日志、控制安全和现场测试
  四页初学者教程；同步 README、配置参考、模板、打包目录和三个仓库测试。
- System Instrument API 升级到 1.2：一个物理仪表实例使用一个地址和一个进程，附加读数
  改为有序 `metrics` 字典；监控卡自动扩展，多台物理仪表按可用宽度换行并并发轮询。
- 新增 `tools/instrument_scanner.py`。它只列出 VISA 资源并发送一次 `*IDN?`，由用户确认
  System/Measurement 用途、实现和主/辅助读数后，原子写入
  `configs/instruments.local.toml`。
- Windows 包新增独立的 `tools/InstrumentScanner.exe`；Release 的 `tools/` 不携带 Python
  源码。缺少 NI-VISA 等 VISA implementation 时会给出明确提示。
- 主配置通过稳定 `resource` ID 选择 System Instrument；Measurement Module 只获得
  Measurement 地址的只读深拷贝，不能取得或绕开 System Instrument 地址。每次 Run 会
  额外保存 `instrument-resources.toml`。
- 所有随附 Measurement Module 已移除各自的 VISA 枚举和地址刷新逻辑；设置页只选择扫描器
  确认过的 Measurement 资源 ID，后台在真正连接和重连时再由核心解析当前地址。
- 外部 System Instrument 必须引用资源表，主配置中的内联地址和未绑定资源都会在启动前
  被拒绝；资源表使用明确的 `system_instrument` 字段，不保留旧字段迁移。

## 0.14.1 - 2026-08-12

- Device Plugin 新增可选 `poll_measurement()`：写测量行和 Module `api.devices()` 时可只
  查询主测量值，常规 `poll()` 继续独立执行完整安全与监视读取。未同步读取的附加列保留
  固定 Schema 但写空，避免把旧监视值伪装成本次测量值。
- 同一台设备的所有访问仍严格串行；已经开始的完整仪表事务先执行完，随后控制/安全操作
  优先，测量专用 `poll_measurement()` 再优先于尚未开始的后台 `poll()`。
- Cryo-con 22C/24C 的测量快照只发送一次 `INPUT B:TEMPERATURE?`；TempA、A/B 报警、
  Loop 状态、加热输出和量程仍由控制轮询检查并写入 `device_status.dat`。

## 0.14.0 - 2026-08-12

- Device Plugin API 升级到 1.1。一个物理设备可以在同一连接、同一快照中返回辅助温度、
  加热输出和量程；这些值显示在设备卡片，并作为 Run 开始时冻结的固定列写入实验 DAT
  与 `device_status.dat`。
- 新增可选的仪表稳定状态。它只能作为核心误差、斜率和 dwell 判断的附加必要条件，不能
  绕过软件独立判稳；设备子进程抛出的 `SafetyViolation` 也会保留安全语义，不再误入普通
  通讯重连流程。
- 前面板常规设备轮询默认改为一秒；SEQ 运行、暂停和停止收尾期间仍以 0.2 秒独立采样
  用于安全检查与判稳，前面板和 Live Trend 不跟随加速。Measurement Module 的
  `api.devices()` 和每条测量行写入前会即时采样；并发请求合并，0.1 秒内刚取得的同一
  样本可以复用，避免连续敲击慢速温控器。
- 配套 Device 仓库新增 Beta Cryo-con 22C/24C 插件：B 为主温度、A 为只读二级冷头温度、
  不提供磁场；Loop 1 使用 Ramp-PID，按目标温度的严格上边界选择 PID，每次写入后立即
  回读确认，并记录实际加热输出 `%FS` 与加热量程。
- Cryo-con 插件连接和退出不改变已手动完成的仪表设置或关闭加热输出；A/B 报警、加热
  故障和 A 超过软件上限会立即停止，普通失联仍使用核心的一分钟恢复窗。该插件尚未经过
  真机验证，因此不随核心 Windows 包预装。
- README、DAT 格式、配置说明、Device Plugin 教程和开发者网站已同步 Device API 1.1、
  即时测量采样及同连接辅助读数的用法。

## 0.13.3 - 2026-08-11

- 新增 **View → Appearance**：整体界面可在 75%–200% 间调整，文字可独立在
  70%–150% 间调整；保存后在下次启动统一应用到核心界面和按需加载的模块窗口。
- 可选择记住窗口尺寸与位置、始终最大化或始终使用默认布局；主窗口、MDI 子窗口、
  Live Trend、手动控制和模块窗口分别保存，并可一键清除全部窗口位置。
- 左右固定侧栏不再显示关闭/浮出按钮；右侧命令栏降低硬最小宽度，辅助文字根据实际
  宽度自动换行，分隔线可在紧凑与宽布局之间自由调节。
- 外观偏好保存在当前 Windows 用户配置目录，不进入仪表配置、SEQ、DAT 或运行快照；
  离屏发布验证固定使用默认外观，避免个人设置污染发布截图。

## 0.13.2 - 2026-08-10

- 主窗口左侧在 `Sequence Status` 下方增加 Enabled Measurement Module 监视卡，显示
  运行/最小化/报警状态及模块选择的最近测量列；点击卡片可恢复独立模块窗口。
- 新增可选 `Module.display_columns` 列名声明。紧凑结果只复用已经通过 Schema 校验的
  测量返回值，不触发额外仪表访问；每轮 `Measure` 先清空旧缓存，空值不会沿用前一轮。

## 0.13.1 - 2026-08-10

- Keithley 2400、2614B 和 6517B Measurement Module 增加默认勾选的
  `Output OFF at SEQ end`。同时取消逐行关闭与 SEQ-end 关闭后，成功测量留下的连续偏置
  可跨 completed、Stop 或 Error 保持，下一次 `run_start` 不会制造短暂掉电。
- 连续偏置只在输出状态及全部关键设置读回一致时保留；前面板改值、读回失败、Measure
  异常、重新 Apply、Disable 或应用退出仍会强制进入各模块定义的安全关闭状态。
- 2614B 额外禁止 Disabled 通道保留输出；6517B 只接受完整的
  operate + zero-check OFF 或 standby + zero-check ON 状态组合。
- 修正 Lake Shore 372A、LR-700 与 Delta 模块的重新 Apply、部分连接失败、关闭失败等
  清理路径，避免旧会话、旧 Applied 状态或未确认的激励状态残留。
- 将开发规范改为“`run_end` 默认关闭；只有明确选择且读回确认后才可保持”，并补充连续
  偏置的测试要求和初学者说明。
- 开发者网站暂时只发布一个稳定入口；移除开发版菜单和 `dev` 发布目录，主分支文档检查
  通过后直接更新稳定站点。
- 重写开发者网站的初学者路线，用日常语言解释模块文件、通道、设置和测试；Measurement
  Module 与温度/磁场 Device Plugin 改为两条独立教程，不再在模块章节混讲。
- 新增基于 MkDocs Material 的中文开发者网站，包含首页、快速开始、Measurement Module、
  Device Plugin、仪表驱动分层、离线发布、排错、安全清单和自动公共 API 页面。
- 网站提供站内搜索、代码复制和 GitHub Pages 自动发布；
  严格构建会在链接、代码片段或 API 文档失效时阻止部署。
- 新增无硬件 `tutorial_resistance` 四通道教程模块及测试，展示 Settings/Status、稀疏
  测量行、数字状态码、rawdata、SI 电流写法和模块自定义普通/扫描 SEQ 指令。

## 0.13.0 - 2026-08-08

- Enabled Measurement Module 可按需声明自己的普通 SEQ 指令和扫描指令；完成 Enable 后
  才加入 Sequence Command Bar，Disable 或 worker 失效后立即移除。
- 模块指令使用稳定模块 ID、指令 ID 和受限 JSON 参数保存；模块缺失时仍可原样读取，
  但编辑器标红且 Run 预检拒绝执行，不会自动 Enable、Apply 或静默跳过。
- 模块 Scan 可与 Temperature、Field、Time 及其他模块 Scan 任意嵌套；核心逐点串行调用
  模块动作，成功后才运行子树，动作本身不生成 DAT 行。
- `Scan Field` 新增可选的近极性模式：运行到该命令时根据当前实际场选择输入路径或整条
  反号路径；距离相同时保留输入方向，选定路径在首个设定点前完成全部安全限制验证。
- 配套 Measurement Module 仓库将仪表协议指令拆分到每台仪表的独立 Python 文件，并把
  6221/2182A 的 7001 与 3706A 路由实现合并为一个按配置选择的 Delta 模块。

## 0.12.0 - 2026-08-08

- Measurement Module 改用不兼容的最小约定式接口：清单仅需 name/version，目录名作为
  ID，入口固定为 `backend.py:Module`，DAT 列由后端 `columns` 提供。
- 后端无需继承基类，只需 `open(api)`、`measure(slot, api)` 和 `close(api)`；Apply、SEQ
  开始/结束、Status 和手动动作统一为可选的 `configure` / `on_event`。
- 删除 manifest 调度模式和入口字段。可选 `slots` 属性声明逻辑槽位，未声明的模块自动
  跟随每个槽位；`measure` 直接返回一行或 `(row, raw_values)`，核心不解释状态码含义。
- Frontend 改为只需 `load` / `dump` 的普通 QWidget；精简模块规范和示例，删除重复的
  设计/验证报告，同时保留进程隔离、协议边界及全部仪表安全测试。

## 0.11.5 - 2026-08-01

- Measurement Module API 升级到 1.1。正式模块通过 manifest 显式声明
  `aligned_slots` 或 `once_per_slot`；缺少字段时 Modules Manager 显示 Warning，并按
  `once_per_slot` 兼容执行。
- 一条 `T Measure` 现在按所有扫描模块的启用逻辑槽位并集展开。CH1–CH4 分别写四行；
  同一槽位的扫描模块并行并合入该通道行，未启用的模块列留空。
- 2400 等单次模块会在每个逻辑槽位重新测量；没有扫描模块时使用唯一槽位 1，因此仍
  只测量并写入一次。
- 每次模块 `measure()` 调用必须恰好产生一行。无行、多行或同时 `emit_row` 与返回
  Mapping 都会被拒绝，避免第三方模块改变通道行数或破坏跨模块对齐。
- 同一逻辑槽位只采一份核心温度、磁场和 Monitor 快照；多个模块仍可把各自原始序列
  写入独立 rawdata sidecar。Stop 取消当前槽位时不写半成品行，前序通道行保留。

## 0.11.4 - 2026-07-31

- Measurement Module 的实验 DAT 状态统一使用整数 `StatusCode`：`0` 固定表示正常，
  其他非负数值及故障优先级由各模块自行定义；人类可读 Warning/Error 只进入界面、
  运行日志和 `events.dat`，不再写入模块数据列。
- 中央会拒绝缺失、文本、布尔或负数 `StatusCode`；示例模块同步移除文本
  `Status`/`Warning` 列并升级到 1.0.2。
- 所有现有模块在非零 `StatusCode` 行中不再保留当前通道的正式测量结果；故障通道和
  未测通道都写为空，仅保留状态码以及仍可信的温场、通道或诊断元数据。
- Measurement Module 的 `emit_row()` 可选携带有限数值 `raw_values`；中央在 Run 的
  `rawdata/` 中按“正式 DAT + 模块”写无表头 sidecar，并保持每条原始行与正式结果行
  顺序对应。
- rawdata 每行最多 32,768 个有限数值，以保证最坏 JSON 表示仍低于 1 MiB IPC
  上限；同名外部 DAT 使用路径摘要区分，`Set Datafile ... create` 会同步重建对应
  sidecar，避免旧行残留。
- Stop 恰好发生在 Measurement Module 的 `begin_sequence`/ARM 等待中时，协作取消
  现在保持正常 Stop 控制流，不再把模块误标为 Faulted 或额外产生 Error。

## 0.11.3 - 2026-07-29

- 修复首次信任并 Enable Measurement Module 时，运行时未重新载入信任记录、可能一直
  停在 Initializing，只有重启应用后才能正常 Enable 的问题。
- 模块初始化的状态轮询与 IPC 超时不再共用 100 ms 上限；所有 Enable 失败路径都会
  回收 worker、恢复 Disabled 状态和界面控件。
- 数值输入框和未展开的下拉框不再响应鼠标滚轮，滚轮继续滚动外层页面；下拉列表展开
  后仍可正常滚动浏览多页选项。
- Temperature List Scan 使用方括号显示显式温度点，并保留每个点输入时的小数位；
  旧版无方括号语法继续兼容且未编辑时保持原文。
- Device Plugin 文档只描述随设备变化的示例和扩展边界，不再写入具体私有仓库信息。

## 0.11.2 - 2026-07-27

- Data Browser 会把可确认来源的绝对时间戳显示为实际日期时间：Quantum Design DAT
  使用 `FILEOPENTIME` 校准，OpenLab DAT 使用新增的 `TIMESTAMP_EPOCH` 标记；
  双击详情同时保留原始秒值。
- 线性坐标主刻度改为 `1/2/5 × 10ⁿ`，时间轴改为整毫秒、秒、分钟、小时或日期，
  不再显示由等分边界产生的 `200.333` 一类刻度。
- `Set Datafile` 参数窗口增加 `Browse…`，按 Mode 直接调用 Windows 原生打开/保存窗口，
  自动补 `.dat` 后缀并保存明确授权的绝对路径。
- Live Trend 改用设备快照采样时间；隐藏时不重绘，可见时把高频快照合并为最多每
  250 ms 一次 GUI 重绘，避免曲线历史反复计算占用界面线程。
- 每个 Run 新增 `device_status.dat`，默认每秒记录全部配置设备的当前值、目标、速率、
  Activity、Stability、Connection、Connected、读数年龄和消息；极短 Run 也保留初始行。
- 数据、事件和设备状态三个默认文件名必须互不相同；`Set Datafile` 不能覆盖运行目录
  中的事件、状态、SEQ、配置或模块设置快照。
- 保存 SEQ 时把当前关联模块和所有 Enabled 模块的界面设置写入同名
  `<序列名>.modules.toml`；再次 Load SEQ 时一同导入。
- 导入模块设置不会自动 Enable 模块、连接仪表或 Apply Settings；已 Enabled 模块只更新
  Settings 页并明确标记为未 Apply。
- 打开运行目录内的标准 `sequence.seq` 时，兼容导入同目录
  `module_settings/*.settings.toml` 快照；旧 SEQ 没有伴随文件时行为不变。
- 伴随文件使用 1 MiB、128 个模块和纯 TOML 数据限制；结构错误时整体拒绝设置导入，
  但仍允许打开 SEQ 文本。

## 0.11.1 - 2026-07-26

- 修复正式 Windows 包中 Lake Shore 372A 被误判为需要私有 PyVISA runtime、因而首次
  Enable 前要求不存在的便携 Python 的问题。
- PySide6、QtAwesome、packaging、PyVISA 1.16.2 和 typing_extensions 4.16.0 改为
  主框架统一锁定并随源码/EXE 提供；模块和设备插件默认使用相同版本。
- manifest 中与框架版本兼容的依赖自动归为共享依赖，不再要求扩展携带重复 lock/wheel；
  不兼容范围在扩展源码导入前 fail-closed。
- `Install Dependencies` 只在所选模块存在真正额外依赖时显示；缺少 runtime marker
  改为明确报告“尚未安装”，不再把正常首次状态写成 invalid marker。
- Lake Shore 372A 升至 0.1.0b3，删除重复的 PyVISA/typing_extensions wheel，并要求
  OpenLab Control 0.11.1 或更新版本。硬件支持仍保持 Beta，未声称通过真机验证。

## 0.11.0 - 2026-07-26

- 将经过 Beta 2 完整仿真、源码端到端和 Windows 干净包验证的核心框架提升为稳定版本。
- 稳定范围包括 SEQ、设备/模块生命周期、DAT/Data Browser、扩展隔离、报警发送和
  Windows 打包契约；扩展 API 保持与 0.11.0 Beta 2 兼容。
- 默认设备仍为仿真；Lake Shore 372A 等未经过真实仪表验证的硬件扩展继续保留各自的
  Beta 版本和真机上线门槛。
- Windows 发布 staging 会移除外置模板、集成和示例中的 Python 缓存，不再把开发机
  运行测试产生的 `__pycache__`、`.pyc` 等文件带入发布 ZIP。

## 0.11.0b2 - 2026-07-26 (Beta 2)

- Measurement Module context 增加实时只读系统快照、Pause 冻结的可中断等待和 Stop
  协作取消；长 pause/dwell 不再阻止 SEQ 及时停止。
- 模块系统快照增加设备 role 与 control 状态，但仍不向模块暴露温度或磁场写入 API。
- 新增有界后台 HTTP 报警发射端；Warning 只路由给测试员，Error 路由给管理员和测试员，
  网络失败不阻塞或改变 SEQ 安全语义。
- 新增 fail-closed 的 NoneBot2 OneBot V11 接收端参考；Token 必填，收件人仅由服务端
  配置，并按稳定事件 ID 对部分重试去重。
- Measurement Module 仓库新增 Lake Shore 372A Beta 模块：GPIB 资源选择、R1-R4
  稀疏多行结果、两次温场平均、状态分类、设置回读和分流确认。
- 模块模板等待统一改用可中断 API；使用该 API 的模块清单要求 OpenLab Control
  0.11.0 Beta 2 或更新版本。

## 0.11.0b1 - 2026-07-26 (Beta 1)

- 新增外部 Device Plugin `device.toml` 发现、API/core 兼容检查、内容树指纹和首次加载信任；
  修改源码、清单、lock 或 wheel 后旧信任自动失效。
- 每个配置设备实例改为独立 spawn 子进程和受 1 MiB 限制的 JSON IPC；阻塞驱动可被
  terminate/kill，不再阻塞其他设备或主运行时。
- 温度/磁场增加 primary/secondary 角色和 `control_enabled`；每种最多一个 SEQ 主控，
  其他设备默认只读，Monitor 永远不可控。
- 读链路失联进入可配置恢复状态，默认每 2 秒尝试、最长 60 秒；SEQ 主设备恢复期间冻结
  活动计时，恢复后核对实际 target/rate。
- 写超时不自动重放；Run 预检要求 primary 已连接并具有新鲜读回；Stop/取消/关闭均尝试
  基于当前读回 Hold，不能确认时最终 Faulted。
- Measurement Module 增加内容指纹信任，Enable 前重新验证源码与依赖 runtime。
- 模块 IPC 从 pickle 改为受大小限制、拒绝 NaN/Infinity/复杂对象的 UTF-8 JSON。
- Device/Module 依赖改为按 type/ID/content fingerprint 隔离；不同扩展可以使用同一包
  的不同版本，依赖不会进入 GUI/核心进程或执行 `.pth`。
- 扩展依赖只允许完全离线安装：精确 `==`、SHA-256 `requirements.lock`、本地 wheels、
  `--no-index --only-binary --require-hashes`，并验证整个 runtime 摘要。
- 依赖安装使用 staging + 原子替换；缺失、版本不符、marker 异常或内容篡改均阻止加载。
- 模块关闭采用 abort + close 的单一总期限；abort 失败仍保留 Error，但有界强制回收本机
  worker 并明确不把进程关闭描述为仪表安全。
- 核心 `modules/` 与 `device_plugins/` 默认空；示例模块移入独立 Measurement Modules
  仓库模板，并新增正式 Device Plugins 私密共享仓库模板。
- 更换温控仪/磁体电源改为复制插件、修改一个配置文件并重启，不再建议为设备维护核心
  分支；首阶段安装流程完全离线且手动复制。
- 同步架构、配置、操作、插件开发、测试计划和安全边界；新增仓库模板与离线依赖契约测试。
- Windows 版本资源改用 PEP 440 解析，可正确构建 `0.11.0b1` 等预发布版本。

## 0.10.3 - 2026-07-26

- 修复活动 Error 被去重后不再中止后续 SEQ 的问题；Warning 仍继续运行并只提醒一次。
- Pause 现在冻结 Wait 与 Scan Time 的运行时钟，恢复后不会跳过尚未完成的等待或时间点。
- Stop/Error 后温度和磁场一律执行 Hold Current；任一控制设备未确认 Hold 时最终状态为 Faulted。
- 为 Connect/Poll/Set/Hold/Disconnect、模块 IPC 与工作进程退出增加有限超时、隔离和强制清理；控制等待在无新读数时也会按配置超时。
- 拒绝非有限设备读数、控制目标、速率和模块测量数值，避免 NaN/Inf 绕过安全比较或污染 DAT。
- 手写 SEQ 与运行时统一验证 Wait、Scan 持续时间、点数、速率和选择项边界，不再静默截断非法值。
- SEQ 温度/磁场命令绑定真实配置设备 ID；参数窗口按所选设备切换上下限和最大速率。
- 修复 DAT `open` 模式误建文件、动态列覆盖旧行、追加 Schema 不一致和多行结果写入完整性问题。
- 默认日志文件名限制在运行目录内，数据/事件文件禁止重名；并发创建同名运行目录时自动重试。
- 修复嵌套 Scan 与 Call Sequence 的进度总数；完成前不再提前显示 100%。
- 修复临时对话框与 Refresh 后模块窗口未及时销毁导致的信号累积和内存增长。
- 源码启动器不再误运行残留旧 EXE；Windows 包不再在 `_internal` 重复嵌入外部可维护资源。
- 新增精确依赖锁定和发布契约测试；Windows 文件版本由程序版本自动生成。
- 删除未被代码或文档引用的重复 DAT 摘录示例。

## 0.10.2 - 2026-07-23

- 模块窗口最小边界改用 Qt 的真实内容 `minimumSizeHint`，不再把包含空白的推荐尺寸锁为最小尺寸。
- 4K 下的固定尺寸下限进一步缩小，参数与按钮仍完整时允许窗口明显更紧凑。

## 0.10.1 - 2026-07-23

- `Apply Settings` 现在只位于模块的 Settings 页，切换到 Status 后不再显示。
- 模块窗口增加随 UI Scale 缩放的内容安全最小尺寸，达到完整布局边界后不能继续缩小。

## 0.10.0 - 2026-07-23

- 将测量仪表从温度/磁场/监视设备体系中完全拆分，移除旧 `measurement` 设备、Transport 状态块和旧 Measure 参数。
- 新增可配置 `modules/` 源码发现、`module.toml` 清单、API 版本检查、依赖检查与共享依赖目录。
- 每个已启用模块使用独立工作进程持有仪表通信；自定义 PySide6 前端留在主界面进程，二者通过受控 IPC 通信。
- 新增 Modules 工具栏按钮和管理器；每次启动全部 Disabled，Enable 成功后才勾选并打开模块窗口。
- 模块窗口为主窗口拥有的独立浮动窗口，固定 Settings/Status 两页、默认 Settings，用户不能直接关闭。
- 实现 `initialize / apply_settings / begin_sequence / measure / end_sequence / abort` 生命周期及失败语义。
- SEQ `Measure` 改为无参数单行，所有 Enabled 模块并行测量，中央程序等待全部完成后继续。
- 模块可流式返回多行结果；中央程序在每次结果到达时采集最新温度、磁场和 Monitor 快照并立即写 DAT。
- DAT 列由运行开始时的模块清单固定并自动加模块 ID 前缀；模块不能直接写实验 DAT。
- Run 前自动保存启用模块的设置，并在运行目录保存设置、实际状态、SEQ 与主配置快照。
- 新增设置未 Apply 时的 `Apply and Run / Run Without Applying / Cancel` 选择。
- 新增共享离线 wheels、显式在线安装确认和依赖冲突禁用规则。
- 修改共享依赖前必须 Disable 全部模块，避免运行中的模块进程加载到被替换文件。
- 新增完整 `simulated_transport` 示例模块：自定义界面、R1–R4 顺序流式四行、手动操作、状态/Warning 列和结束清理。
- 新增测量模块模板、专项自动测试、界面预览及模块开发/依赖/上线工作流文档。

## 0.9.2 - 2026-07-23

- SEQ 的 Set/Scan Temperature 与 Set/Scan Field 参数弹窗现在按 `device_id` 读取配置文件中的 `min_value`、`max_value` 和 `max_rate_per_minute`。
- 目标、扫描起止点和速率输入框直接使用配置范围，并在弹窗底部显示当前设备的有效限制及单位。
- 磁场参数在 Oe/T 间切换时同步换算当前数值和配置限制，例如默认 ±90000 Oe、10000 Oe/min 对应 ±9 T、1 T/min。
- Scan Temperature List 在弹窗确认时逐点检查配置上下限；越界点立即提示并阻止确认，执行器的整表预检仍作为第二道保护。
- 增加主窗口配置传递、四类控制命令范围、磁场限制换算和温度 List 越界拦截回归测试。

## 0.9.1 - 2026-07-22

- 左侧 `Change` 选择的 DAT 现在可直接写入用户指定文件夹，并在 SEQ 中以 `external` 标记持久化该次明确授权。
- `Set Datafile` 参数窗口增加 `Run folder / Custom folder`；旧 SEQ 中未标记的外部绝对路径仍按配置安全重定向，避免放宽全局路径策略。
- SEQ 和 DAT 文件对话框在当前会话内记住最近使用的目录；缺少 `.seq`/`.dat` 后缀时自动补齐。
- 左侧 DAT 路径与 SEQ 文件名改为单行中间省略，完整内容保存在悬停提示中，不再参与 Dock 最小宽度计算。
- 左侧栏基础最小宽度从 225 调整为 205 个缩放单位，长路径不会再把侧栏撑大后锁死。
- 增加自定义目录执行、旧路径安全重定向、`external` SEQ 往返以及长文件名侧栏布局回归测试。

## 0.9.0 - 2026-07-22

- `Scan Temperature` 参数窗口增加 `Linear` 与 `List` 两种点位定义；切换模式时只显示对应的起止点/点数或逗号分隔温度列表。
- 增加单行语法 `Scan Temperature List 300.000, 250.000, 20.000 K at 5.000 K/min, Settle`，仍可包含任意层级子命令。
- List 严格保留用户输入顺序和重复温度点，不自动排序、去重或插值；保存时统一为三位小数。
- 解析器拒绝空项、非数字、非有限数和超过 100,000 点的列表；弹窗在确认前直接提示输入错误。
- 执行器在移动设备前预检整份列表及速率，任一目标超出配置限值时以 Error 中止且不先执行部分列表。
- 增加可运行示例 `examples/temperature_list.seq` 及解析、界面、执行顺序、重复点和整表安全预检回归测试。

## 0.8.3 - 2026-07-22

- 修复关闭浮动 SEQ 子窗口后再点击 New/Open 只出现灰色区域、没有文本编辑器的问题。
- 新建或载入文档时同时恢复 MDI 子窗口与其内部编辑器，并把 SEQ 窗口置为当前活动窗口。
- 增加“关闭 SEQ → New → 编辑器和 End Sequence 重新出现”的 GUI 回归测试。

## 0.8.2 - 2026-07-22

- 将默认磁场设备的原生单位从 T 改为 Oe，并把范围、速率、判稳阈值和仿真噪声按 `1 T = 10000 Oe` 等比例换算，物理边界保持不变。
- 新建 Set/Scan Field 命令默认使用 Oe；Oe 磁场统一显示和保存两位小数，温度统一为三位小数。
- 参数窗口在 Oe/T 之间切换时自动换算目标、扫描端点和速率，避免仅改单位标签造成 10000 倍误差。
- 继续兼容旧 T 单位 SEQ；编辑 T 命令时保留六位小数，用户提供的原始模板仍逐行原样往返。
- DAT 控制列、状态卡、手动控制窗口和扫描路径采用一致精度，并增加默认单位、精度及弹窗换算回归测试。
- 修复并发轮询的旧快照可能覆盖刚设置目标的竞态，避免快速小步进 Scan 在旧目标处提前测量；固定小数格式不再显示负零。

## 0.8.1 - 2026-07-22

- 增加按屏幕原生分辨率自动计算的界面缩放：1080p 为 1.00×、2K 约 1.15×、4K 为 1.40×。
- 全局字体、工具栏图标、状态卡片、手动控制/告警弹窗、浮动窗口及数据图表边距同步缩放，避免仅放大字体造成裁切。
- 在 `[application]` 中增加 `ui_scale = "auto"`；也可设置 `0.75` 到 `2.0` 的数值手动覆盖。
- 状态栏和 About 窗口显示本次使用的缩放倍率及 Auto/Manual 模式。
- 增加自动缩放、手动覆盖和配置边界的回归测试，并完成 1.40× 主窗口与 Data Browser 视觉检查。

## 0.8.0 - 2026-07-22

- 整合新版桌面前端：工具栏使用 QtAwesome 矢量图标，状态区采用更清晰的浅色层级、状态徽章和强化数值显示。
- 将全局字体规范为 10pt，在保持设备读数醒目的同时避免菜单、命令栏和 SEQ 文本过度放大。
- 修复包含长命令时 SEQ 列表可能自动横向滚动、导致每行开头被截断的问题；每次重建后恢复到最左侧。
- 浮动 SEQ 和 Data Browser 窗口会在主窗口缩放后保持于中央工作区内，避免常见小屏分辨率下左边缘被裁掉。
- 保留 `open_env.bat` 和 `run_console.bat` 作为源码开发辅助入口。
- 移除已引入但未使用的 PyQtDarkTheme 依赖，仅保留实际使用的 QtAwesome。
- 恢复 Git 忽略规则并清理运行日志、冒烟截图和临时工作目录等生成文件。

## 0.7.0 - 2026-07-20

- 增加通用只读 `monitor` 设备类型，并在默认配置中加入 `2nd Stage` 温度监视器。
- `2nd Stage` 仅显示当前值和连接状态，可加入 Live Trend；不显示目标或速率，不打开手动控制窗口。
- Monitor 不属于主温度控制类型，不参与 Set/Scan Temperature、数值判稳、中止 Hold 或输运仿真的标准温度选择。
- 对任何 Monitor 发送目标都会以 `TARGET_NOT_CONTROLLABLE` 拒绝，避免配置或 SEQ 误操作。
- 增加真实只读仪表插件模板，并补充 Monitor 配置、插件工作流、界面和安全边界测试。

## 0.6.0 - 2026-07-20

- Data Browser 右键菜单以 `Select Y Series...` 打开持续显示的多选窗口，可连续勾选任意多列后一次确认，不再每选一列就关闭菜单。
- 增加独立 `X Scale` 和 `Y Scale` 菜单，可分别选择 Linear 或以 10 为底的 Logarithmic 显示。
- 对数坐标自动忽略非正数点，并在非正数区间断开曲线；若没有可绘制正值，图内显示明确提示。
- 对数坐标支持框选放大、数据点命中、多 Y Overlay 和 Stacked / Shared X。
- `.plt` 格式升级到版本 2，保存 X/Y 尺度；旧版版本 1 文件继续按 Linear 方式读取。
- 增加多 Y 批量选择、对数变换、非正值处理、PLT 往返和旧格式兼容测试。

## 0.5.0 - 2026-07-20

- SEQ 列表支持 Windows 标准多行选择：`Ctrl+Click` 增减单行，`Shift+Click` 连续选择。
- Disable、Enable、Delete 和 Copy 可一次作用于全部选中命令；键盘快捷键与右键菜单行为一致。
- 多行 Copy/Paste 保持文档中的原始顺序，并在粘贴后选中所有新建的顶层副本。
- 当父 Scan 与其后代、或 Scan 开始行与 End Scan 同时被选中时，结构性 Copy/Delete 只处理父节点一次，避免重复复制或删除。
- 右键已选中的任意行会保留整组选择；右键未选中行时切换为只选择该行。
- 增加多行批量编辑、层级去重、批量粘贴顺序与选择恢复的自动测试。

## 0.4.0 - 2026-07-20

- SEQ 窗口增加右键 `Disable`、`Enable`、`Delete`、`Copy` 和 `Paste`。
- 增加 `Ctrl+D` 禁用、`Ctrl+E` 启用、`Delete` 删除、`Ctrl+C` 复制、`Ctrl+V` 粘贴快捷键。
- Copy/Paste 以完整命令节点为单位；Scan 会连同任意层级子命令一起复制。
- 禁用行使用 MultiVu 风格 `F ` 前缀保存，启用行使用 `T `；重新加载后状态保持。
- 执行器跳过禁用命令；禁用 Scan 时整个子树不执行，并在事件日志记录一次跳过信息。
- 禁用命令及其受父 Scan 影响的子命令以灰色删除线显示。
- 增加解析往返、执行跳过、右键菜单、快捷键、编辑锁和 Scan 深复制测试。

## 0.3.0 - 2026-07-20

- Data Browser 支持在同一图中叠加任意多条 Y 曲线。
- 增加纵向多图布局：各 Y 子图独立纵轴范围，所有子图共享 X 轴和 X 缩放范围。
- 右键 `Y Series` 改为多选；布局可从顶部选择框或右键菜单切换。
- 轴选择、布局和缩放范围自动保存为 DAT 同目录的同名 `.plt` 文件，并在再次打开 DAT 时恢复。
- 兼容读取附加命名形式 `sample.dat.plt`，规范写出形式为 `sample.plt`。
- 增加 `.plt` 格式、叠加/纵向绘图、共享 X、独立 Y 和状态恢复的自动测试。

## 0.2.0 - 2026-07-20

- 增加与测量输出解耦的 MDI Data Browser，可打开或拖入任意 `.dat`。
- 增加 0.75 秒文件变化监视与自动刷新，并在刷新时保留人工缩放范围。
- 增加右键 X/Y 轴选择、Row Number 横轴、框选放大和 Reset Zoom。
- 增加数据点双击命中及完整行详情窗口。
- 增加通用 `[Data]` CSV 解析器、稀疏通道支持和对应自动测试。

## 0.1.1 - 2026-07-20

- 将菜单、命令栏、状态卡片、参数窗口、手动控制和告警界面统一为英文。
- 将默认设备显示名称和运行时用户消息调整为英文。
- 保留中文操作、技术和插件开发文档。

## 0.1.0 - 2026-07-20

- 建立设备能力接口和动态插件加载。
- 加入仿真温度、磁场和四通道电阻测量设备。
- 加入配置化判稳、安全限制和中止保持策略。
- 加入单行 `.seq` 编辑、解析、嵌套执行和 `.dat` 记录。
- 加入 Warning/Error 生命周期与弹窗去重。
- 加入 MultiVu 风格 PySide6 桌面界面和基础趋势图。
- 保留用户提供的 SEQ/DAT 原始模板，并加入兼容性示例。
- 提供免安装 Python 的 Windows x64 打包版本及发布验证报告。
