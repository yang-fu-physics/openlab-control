# 操作手册

这份手册面向日常使用 OpenLab Control 做实验的操作员，按实际实验顺序说明界面布局、模块管理、序列编辑、运行控制、画图和常见异常处理。

## 日常实验 5 步速览

1. **检查底部仪表**：确认已配置的系统仪表面板连接正常、读数合理（全新安装没有任何面板也是正常的）。
2. **加载或编写 SEQ**：打开 `.seq` 文件（如有配套 `.modules.toml`，参数会自动填进对应模块界面）。
3. **启用并应用测量模块**：在 **Modules** 中勾选本次要用的模块，核对 **Settings** 后点击 **Apply Settings** 下发到仪表。
4. **选择输出文件并运行**：启动程序或切换 SEQ 后，左侧数据文件栏默认为空；先点击 **Change**（或编辑 `Set Datafile`）明确选定输出 DAT（已有文件默认续写并校验列结构，新路径自动创建），再点击 **Run**；实验进行中可随时打开 **Data Browser** 看实时曲线。
5. **归档结果**：实验结束后，建议连同 `runs/<时间>_<序列名>/` 整个目录一起备份（对外分享前记得脱敏其中的 `configuration/`）。

---

## 启动方式

### Windows 发布包

将整个发布文件夹解压到可写目录后，直接双击 `OpenLabControl.exe` 启动。请不要单独移动 EXE，需保持同级的 `configs/`、`docs/`、`modules/`、`system_instruments/`、`module_data/`、`runs/` 和 `_internal/` 完整。

### 源码与命令行启动

```powershell
# 日常启动（自动使用 .venv 中的锁定依赖）
.\setup.bat
.\run.bat

# 指定启动时打开某个 SEQ
.\.venv\Scripts\python.exe run.py --sequence examples\nested_scan.seq

# 直接以 Data Browser 打开一个 DAT 文件
.\.venv\Scripts\python.exe run.py --data-file C:\Data\sample.dat

# 无界面自动化验证（仅用于测试，正常 GUI 启动不会自动勾选模块）
.\.venv\Scripts\python.exe run.py --headless-demo --enable-module simulated_transport --sequence examples\module_measurement.seq --timeout 30
```

> **安全约定**：每次重新启动程序，所有测量模块都默认恢复为 `Disabled`，需要操作员主动勾选启用。

---

## 主窗口布局

主窗口按功能分为五个区域：

- **左侧控制区（Sequence Control）**：显示当前数据文件与 SEQ 名称、运行进度、已启用模块的**实时监视卡**，以及底部的 `Run / Pause / Stop` 按钮。
- **中央工作区**：放置浮动的 SEQ 编辑器，以及可同时打开多个文件的 **Data Browser** 绘图窗口。
- **右侧指令栏（Sequence Command Bar）**：双击任意指令弹窗填参数并插入 SEQ；启用带自定义指令的模块后，这里会自动出现该模块的专属分组。
- **底部系统仪表面板条**：按扫描器设定的全局顺序排列所有启用的固定面板（固定浅色配色，不随系统深色模式变色）：
  - **控制面板（`controller`）**：显示当前值、目标值、速率和稳定状态；双击打开手动控制窗口。
  - **只读面板（`readout` / `readout_grid`）**：显示 1 个或最多 4 个（2×2）只读读数，不可双击控制。
  - **开关面板（`switch`）**：显示 `On/Off` 状态并提供无参数控制按钮（例如压缩机开关）。
  - **自定义面板（`custom`）与 Instrument Setup**：支持驱动提供专用底部卡片；声明了独立配置界面的系统仪表（如温控仪切换传感器曲线、软件 PID 表）可通过菜单 **Instrument → Instrument Setup…** 或右键点击其底部卡片打开独立窗口，仅在打开/刷新时按需查询且写操作仅限 SEQ 空闲时生效。
- **辅助工具与视图**：工具栏 **Modules**（模块管理器）、菜单 **View → Run Log**（完整运行与报警记录）、**Live Trend**（轻量实时趋势图，复用已有轮询缓存，最多每 250 ms 刷新一次显示，不增加额外仪表通讯）。

### 调整界面缩放与窗口位置

打开 **View → Appearance**：

- **Overall size（75%–200% 或自动）**：同步缩放按钮、图标、间距、窗口最小尺寸和文字；
- **Text size（70%–150%）**：在整体缩放基础上再单独放大或缩小文字；
- **At startup**：选择记住上次窗口尺寸位置、始终最大化，或恢复默认布局；
- **Reset Window Positions**：换显示器或窗口跑到屏幕外时，保存并重启即可复位全部窗口位置。

设置保存在本机 Windows 用户配置目录，不影响项目内的 `configs/*.toml`、SEQ 或实验数据；点击 **Save** 后重启软件生效。点击 **Restore Defaults** 会回到 `configs/general.toml` 中 `application.ui_scale` 的默认值与 100% 字号。

---

## 管理测量模块（Modules）

点击工具栏 **Modules** 打开模块管理器，界面简洁，只有 `Enabled / Name / Version` 三列。

### 启用（Enable）、多实例与隐藏窗口

1. 勾选目标模块后，状态会显示 `Initializing <模块名>...`；初始化成功即弹出该模块的独立浮动窗口。如果初始化失败会弹 Error，保持未勾选状态。
2. **多台相同测量仪表（Add / Remove Instance）**：如需同时使用多台同型号仪表（如两台 Keithley 2400 或两台 OE1022D），在 SEQ 空闲时点击底部 **Add Instance…** 选择基础模块并指定唯一实例 ID 与显示名即可，**无需复制模块文件夹，也不需要先 Disable 其他已启用模块**；配置保存在 `configs/module_instances.toml`。停用后的额外实例可选中点击 **Remove Instance** 移除（详见 [多台相同仪表复用一个模块](development/reuse-identical-instruments.md)）。
3. 只应将来源可信、已核对过代码的模块目录复制到 `modules/` 下；程序不在勾选时额外弹代码审核确认框。
4. 勾选成功后，如果该模块声明了自己的 SEQ 指令，右侧指令栏会立刻出现以模块名称命名的分组；取消勾选或进程失效时立即移除。
5. **窗口关闭与隐藏**：点击模块窗口右上角关闭、`Alt+F4` 或最小化都会**隐藏**该窗口（不会中断正在进行的测量，也不会在桌面留下最小化小条）。左侧 **Sequence Status** 下方的模块卡片会标出 `Hidden` 并继续显示最新通道结果（新一次 `Measure` 开始前会先清空旧值，空值显示 `—`）。点击卡片或在管理器里双击模块名即可重新打开原窗口。

### Settings、Status 与 Apply Settings

- 打开模块窗口默认停在 **Settings** 页，并自动载入上次保存的参数（位于 `module_data/<模块ID>/settings.toml`）。
- **加载 ≠ 下发**：勾选 Enable 或加载历史 SEQ 只把数值填在界面上，**绝不会自动写给真实仪表**。确认参数无误后点击 **Apply Settings** 并确认，才会真正连接/配置仪表。
- **Status** 页展示仪表回读的连接状态、输出开关与最近读数（仅在 Settings 页显示 Apply 按钮）。部分模块提供的 `Test Connection`、`Read Now`、`Measure Now` 仅在 SEQ 空闲时可用，结果不会写入实验 DAT。

### SEQ 配套模块设置文件（`.modules.toml`）

执行 **File → Save** 保存 `experiment.seq` 时，程序会自动在同目录生成 `experiment.modules.toml`，记录本次实验关联的模块参数。

执行 **File → Open** 打开该 SEQ 时，会自动把配套参数载入对应模块的 Settings 界面（打开历史 `runs/` 目录下的 `sequence.seq` 时，也会兼容读取其 `module_settings/*.settings.toml`）：

- 不会替你勾选或取消勾选模块；
- 对已经 Enabled 的模块，只更新界面并标为“未 Apply”，需要核对后手动 Apply；
- 若本机缺少对应模块或版本不同，会提示 Warning，原参数仍保留供参考；
- 复制实验脚本给同事时，记得把 `.seq` 和同名 `.modules.toml` 一起复制。

### 点击 Run 时有未 Apply 的设置怎么办？

如果某个已启用模块的界面有改动但尚未下发，点击 **Run** 会弹出三个选项：

- **Apply and Run（推荐）**：先将所有改动下发并等待仪表确认，全部成功后再开始跑序列；
- **Run Without Applying（高级）**：不给仪表发设置，按仪表当前实际状态直接跑，但运行快照中仍会记录界面当前填写的期望值——仅当你已经人工确认仪表状态且清楚两者差异时再使用；
- **Cancel**：取消本次启动。

### 停用（Disable）与刷新（Refresh）

- **Disable**：取消勾选模块时，程序会先保存当前 Settings，再调用模块 `close()` 安全收尾，最后关闭子进程并隐藏窗口。如果 `close()` 报错，框架会在超时上限内强制回收本机进程并显示 Disabled——**强制结束进程不等于外部仪表已关断输出，此时务必人工检查仪表实体面板！**
- **Refresh**：仅在 SEQ 空闲且**全部模块都已 Disable** 时可用，用于重新扫描 `modules/` 目录。所有模块共用核心锁定的 Python 依赖，界面不提供单独装包功能。

---

## 编辑实验序列（SEQ）

### 基本编辑

- **新建 / 打开 / 保存**：通过 **File → New / Open / Save / Save As**（若不小心关掉了浮动 SEQ 窗口，再点 New/Open/Edit 即可唤回）。
- **插入指令**：在右侧 **Sequence Command Bar** 双击指令，在弹窗中填好参数即可插入；选中 `Scan` 或 `End Scan` 行再插入时，新指令会自动缩进到该扫描块内部。
- **修改已有指令**：直接双击 SEQ 里的任意一行。
- **批量操作**：按住 `Ctrl` 或 `Shift` 选中多行，可通过右键菜单或快捷键执行 **Enable / Disable / Delete / Copy / Paste**（同时选中父 `Scan` 和内部子行时，操作自动只作用于外层块，避免误删或重复复制）。SEQ 运行中禁止修改，但仍允许复制。

### 常用指令要点

- **Set Datafile**：可点击 `Browse…` 用系统文件对话框选路径（`open` 选已有文件，`create` / `open|create` 可新建）。写到项目外的自定义目录时会自动加上 `external` 标记。
- **温度 / 磁场控制与扫描**：参数弹窗直接读取扫描器中该控制器面板的上下限和最大速率。`Scan Temperature` 支持 `Linear`、`Log`、`List` 三种点定义；`Scan Temperature` 与 `Scan Field` 均支持直接输入 `Step`（`Log` 模式下为 `Points / decade`）与 `Points` 双向联动（输入步长后取整同步点数并回写实际步长，非法步长会保留原文并在确认时拦截提示）。`Scan Field` 额外提供 **Choose nearer start or its negative** 选项（默认关闭）：勾选后，执行到该扫场步骤时会读取当前真实磁场，自动选择离当前场更近的起点极性（例如当前在 `-6 T` 时，把 `9 T → 3 T` 自动转为 `-9 T → -3 T`），结果记入 Run Log。
- **Measure**：无参数指令 `T Measure`，自动对本次 Run 冻结的全部已启用模块按通道槽位测量。需要重复采集或定时采集时，把 `Measure` 放进 `Scan Time` 或其他 `Scan` 块内即可。
- **模块自定义指令**：如 `Set Current`（单次动作）和 `Scan Current` / `Scan Voltage`（可像温场 Scan 一样嵌套子指令，并支持 `Linear / Log / List`、`Step ↔ Points` 双向联动及自动同步模块 Settings 上限）。它们本身不写 DAT 行，需要采数据时在下方或循环内放置 `Measure`；修改的只是本次运行中的仪表状态，不会改写 Settings 页。如果打开的 SEQ 引用了未安装或未启用的模块指令，该行会标红并给 Warning，运行前会被阻止。

### 参数精度说明

为避免软件悄悄把参数四舍五入（例如把极小步长舍成 0），界面输入、保存与执行使用统一精度：

- `Wait`、`Scan Time` 及可编辑等待/超时时间：秒，1 位小数（仪表滤波参数保持各自规定）；
- 普通 NPLC：1 位小数（Delta PLC 和 372A 整秒参数仍为整数）；
- 温度：常规 3 位小数（若仪表清单声明了低温加密精度，如 10 K 以下 4 位，则以仪表清单为准）；
- 磁场：`Oe` 2 位小数，`T` 6 位小数；
- 电流/电压支持 `1m`、`1p` 或科学计数法，缩短会丢精度时原样保留文本。

外部手写的 SEQ 或配置如果超出了控件允许的小数位，程序会提示修正，不会静默四舍五入。

---

## 运行控制、手动控制与报警

### 运行与暂停 / 停止

点击 **Run** 后，SEQ 编辑器、模块启停与设置页面都会锁定，本次参与测量的模块和列结构即固定下来；编辑器会以琥珀黄色高亮并自动定位当前执行行（暂停时保留，停止或结束后清除，不改变手动选中项）：

- **Pause**：在检查点暂停推进和等待计时，不断开仪表、不关输出；点 **Resume** 继续。
- **Stop**：取消后续步骤，向模块发出 `run_end("stopped")` 收尾，但**不向系统仪表发 Set 或 Hold**（温控与磁体保持当前仪表内目标）。
- **主控读链路短时断线**：底部状态转为 `Reconnecting`，默认每 2 秒自动重连、最多尝试 60 秒（期间暂停 SEQ 判稳计时）；连回后会核对实际目标与速率再继续，超时则报错停机。写命令超时不会自动重发。

### 手动控制温度或磁场

SEQ 空闲时，双击底部 `controller` 面板（例如 Temperature 或 Magnetic Field）即可弹出手动控制窗口，填写 **Target** 与 **Rate** 后点击 **Set**，或点击 **Hold Current** 将目标锁定为当前新鲜读数。

- 上下限和最大速率与扫描器配置完全一致，运行时会再次校验；
- 只读面板（如 `2nd Stage`）不能双击控制；角色设为 `none` 的控制面板允许手动操作，但不会被标准温场 SEQ 选中。

### Warning 与 Error 的区别

| 等级 | 弹窗标题 | SEQ 是否继续 | 典型场景 |
| --- | --- | --- | --- |
| **Warning** | `Warning / Operation Continues` | **继续运行**（当前异常点留空，有效数据照常写） | 单点超量程、触发 compliance、无启用模块 |
| **Error** | `Error / Operation Stopped` | **立即中止（Faulted）**，已写数据保留，模块收到 `run_end("error")` | 仪表掉线超时、互锁异常、传感器报警、协议错误 |

同一来源、代码与上下文的持续告警只会弹一次窗并在日志里累加计数，恢复（`RESOLVED`）后若再次发生才会重新弹窗。

> **跨仪表联动响应**：系统仪表可以声明特定错误码触发的联动动作（例如某报警触发磁场归零），由核心按最高优先级独立执行并在解除前锁定目标。当前正式系统仪表默认不注册跨仪表联动。

### 可选远程 HTTP 报警通知（NoneBot 等）

如需把 Warning / Error 异步推送到 QQ 或通知服务：

1. 在独立接收服务中开启 `/alarm/report`，接收 `X-Token` 请求头及 JSON 中的 `message`、`target_qq`。
2. 打开顶部菜单 **Settings → Alarm Notifications…**，填写设备名称、完整 Service URL、Token 和接收 QQ，勾选启用。消息显示为 `[设备名]：实际错误内容`。使用远程 HTTP 服务时勾选 **Allow HTTP**；HTTPS 无需此项。
3. 点击 **Send Test Warning** 发送一条明确标注为测试、无实际仪表故障的 Warning。测试使用窗口中的当前输入，不保存设置；服务器接受后显示成功，实际 QQ 送达由接收端完成。
4. 点击 **Save** 保存并立即生效，成功后窗口关闭。设置保存在本机用户配置目录的 `alarms.json`，Token 单独保存在 `alarm-token.txt`；它们不进入 SEQ、DAT 或运行配置快照。下次打开程序自动读取这组本机设置。

**接收 QQ 留空即关闭远程通知**，即使原配置为启用也不发送；测试按钮同时禁用。Warning 和 Error 均发送到设置中的同一个 QQ，级别保留在消息中。服务端自行处理额外路由规则，本框架不设置管理员／测试员 QQ 列表。

无界面运行使用 `configs/general.toml` 中的 `[alarms.reporting]`，通过 `target_qq` 指定接收人，并通过 `token_env` 或 `token_file` 引用凭据；GUI 本机设置不改写这个 TOML 文件。

报警网络失败只会产生本地 Warning，绝不会阻塞实验，也不能代替硬件联锁。

---

## 查看数据与运行归档

### Data Browser

- 点击左侧数据区 **View**：新建窗口并直接打开当前 Run 的实验 DAT；
- 点击菜单 **Graph → Data Browser**（或直接把 `.dat` 拖进主窗口）：打开任意历史 DAT，可同时开多个窗口对比，文件继续写入时自动刷新；
- 在图上右键可选 X 轴、一次性勾选多个 Y 轴、切换 **Overlay / Stacked**、开关对数坐标或 **Reset Zoom**；拖框放大，双击最近数据点查看该行完整原始字段，布局自动保存到同名 `.plt`。
- 能确认时间基准的 `Timestamp(s)` / `Time Stamp (sec)` 在线性轴上会自动显示为真实日期时间（兼容 OpenLab 与 Quantum Design DAT）；无法确认基准时保留原始秒数，不盲目猜日期。

详细画图说明见 [用 Data Browser 查看 DAT](guides/data-browser.md)。

### 每次 Run 的归档目录

```text
runs/<时间>_<序列名>/
├─ experiment.dat                        # 实验主数据
├─ instrument_status.dat                 # 每秒连续仪表状态（不随外部 Set Datafile 移走）
├─ events.dat                            # 完整事件与报警记录
├─ sequence.seq                          # 实际执行的 SEQ 快照
├─ configuration/                        # general.toml、visa.resources.toml、instruments/、pid/ 快照
├─ module_settings/*.settings.toml       # 模块期望参数快照
├─ module_settings/*.status-at-start.json# 运行前读回的模块实际状态
└─ rawdata/                              # 模块输出原始序列时生成
```

实验结束后建议整文件夹打包备份，复现实验时直接打开其中的 `sequence.seq` 即可恢复参数视图。对外分享前务必检查 `configuration/` 并删除本机地址或隐私路径。

### 关闭程序

运行中关闭主窗口会先弹出确认。确认后程序先请求停止 SEQ 并等待模块完成收尾（例如源表降流与关断）；若收尾失败（`Faulted`）会保持窗口打开以展示错误，收尾完成后再依次保存已启用模块的 Settings → 对每个模块调用 `close()` → 断开系统仪表 → 关闭日志并退出。下次启动时所有模块仍恢复 `Disabled`。

---

## 常见问题速查

- **模块勾选不了**：在 Modules 窗口选中该行看底部报错，通常是 `module.toml` 或 `backend.py` 有语法/字段问题；修好后先 Disable 全部模块再点 **Refresh**。
- **Enable 后参数没生效**：这是安全设计，必须人工核对后点 **Apply Settings** 才会发给仪表。
- **模块窗口关不掉**：点关闭只会隐藏窗口并保持运行；要彻底关闭模块，请在 Modules 管理器取消勾选。
- **Disable 时报错但变成了 Disabled**：框架已强制回收工作进程，但不代表真实仪表输出已关断，请立即到实体面板检查。
- **Measure 没写某模块的数据**：检查 Run 前是否已勾选该模块、该通道是否已启用，或该点是否触发 Warning 留空。
- **仪表一直显示 Reconnecting**：恢复窗口（默认 60 秒）内请先检查物理线缆与仪表面板，不要反复点击 Set/Hold。
- **想更换温控仪或磁体电源**：把新驱动完整目录复制到 `system_instruments/`，运行 **Instrument Scanner** 重新配置并检查完整保存预览，再重启主程序。

更多排错步骤见 [常见问题与排错](guides/troubleshooting.md)。
