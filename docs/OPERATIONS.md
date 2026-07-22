# 操作手册

## 安装与启动

### Windows 发布包（推荐直接体验）

1. 解压 `OpenLabControl-Windows-x64.zip` 的全部内容。
2. 双击 `OpenLabControl.exe`。
3. Windows 首次运行若显示信誉提示，请核对压缩包校验值和来源后再决定是否运行。

发布包已带 Python 与 PySide6 运行组件，无需另装 Python。请勿只复制 EXE；`_internal`、`configs`、`examples`、`docs` 和 `plugin_templates` 应与它保持原有相对位置。

### 本机已准备的版本

项目包含隔离环境时，双击 `run.bat` 即可启动。

若目录中已有 `dist/OpenLabControl/OpenLabControl.exe`，`run.bat` 会优先启动打包版本。

前端或插件开发时：

- `run_console.bat`：强制从源码启动，关闭程序后保留控制台和真实退出码，便于查看导入或启动错误。
- `open_env.bat`：打开已激活 `.venv` 的命令行，便于运行测试和构建命令。

### 在另一台电脑首次启动

以下步骤仅适用于源码包；使用 Windows 发布包时不需要：

1. 安装 Python 3.11 或更高版本。
2. 双击 `setup.bat`，等待依赖安装完成。
3. 双击 `run.bat`。

命令行方式：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

无界面完整演示：

```powershell
.\.venv\Scripts\python.exe run.py --headless-demo
```

发布验收记录见 `docs/VERIFICATION_REPORT.md`。

## 主窗口

- 左侧：数据文件、当前 SEQ、运行状态以及 Run/Pause/Stop。
- 中间：每行一条指令的 SEQ 编辑窗口。
- 右侧：分组命令栏，双击命令插入。
- 底部：由配置中的设备动态生成状态块。
- 默认的 `2nd Stage` 是只读辅助温度：显示当前值和 `Monitoring`，不显示目标/速率，双击不会打开控制窗口，也不参与主温度判稳。

SEQ 是可关闭的浮动子窗口。关闭后可点击工具栏 `New`、`Open`，或左侧 Selected Sequence 下的 `Edit` 重新显示；外框和内部文本编辑器会一起恢复。
- 菜单 `View → Run Log`：显示命令、Warning 和 Error。
- 菜单 `Graph → Live Trend`：打开本次运行的临时监视曲线。
- 菜单 `Graph → Data Browser`：打开独立浮动 DAT 数据浏览器。

## Data Browser

Data Browser 与实验正在写入的 DAT **没有自动绑定关系**。它只跟踪用户明确打开或拖入的文件；随后开始新的测量也不会擅自切换浏览文件。

打开方式：

1. 点击工具栏 `Data Browser`、菜单 `Graph → Data Browser`，或左侧 Data File 的 `View`。
2. 把任意 `.dat` 文件拖入浏览窗口；也可点击 `Open DAT` 选择文件。
3. 窗口标题和顶部路径显示当前实际浏览的文件。

操作：

- 文件发生写入、追加或替换后，浏览器每 0.75 秒检查一次，并自动重新读取曲线。
- 在图内右键，通过 `X Axis` 选择任意数值列；X 轴也可选 `Row Number`。
- 右键选择 `Select Y Series...`，在保持打开的窗口中连续勾选任意多列，再按 `OK` 一次应用；至少选择一条 Y 曲线，`Cancel` 不改变现有显示。
- 右键 `X Scale` 或 `Y Scale`，可分别选择 `Linear` 或 `Logarithmic`（Log10）。
- 顶部 `Layout` 或右键 `Layout` 可选择 `Overlay`（多条 Y 叠加在同一图）或 `Stacked / Shared X`（一列多图）。
- 按住鼠标左键框选区域进行放大；点击 `Reset Zoom` 或右键 `Reset Zoom` 恢复全图。
- 双击曲线附近的数据点，弹出该数据行的文件名、行号、X/Y 数值及所有原始字段。
- 稀疏通道中的空单元格不会当作零；选择某个 Y 列时只绘制该列有数值的行。

`Overlay` 使用一组共同的 Y 范围，适合量纲和数量级相近的曲线。`Stacked / Shared X` 中各子图使用独立 Y 范围，X 范围完全共享；在任一子图框选时会同时改变所有子图的 X 视野，只改变被框选子图的 Y 视野。

Logarithmic 使用以 10 为底的对数坐标。对应轴上等于零或小于零的数据点不会绘制或参与双击命中，曲线会在这些点处断开；若没有可用正值，图内会显示提示。切换 Linear/Logarithmic 时会清除原人工缩放，避免把线性范围误用于对数坐标，之后仍可正常框选放大。

显示设置自动保存到 DAT 所在文件夹：

```text
sample.dat
sample.plt
```

`.plt` 保存布局、X 列、多选 Y 列、X/Y Linear 或 Log10、共享 X 缩放、叠加 Y 缩放和每个纵向子图的 Y 缩放。再次打开 `sample.dat` 时自动恢复。`Save PLT` 可显式保存，右键 `Reload Plot Format` 可重新读取外部修改后的设置。程序也兼容读取旧式附加命名 `sample.dat.plt`；版本 1 PLT 按 Linear 读取，但规范写出始终为版本 2 的 `sample.plt`。

实时刷新时，若尚未人工放大，坐标范围随新数据自动扩展；人工放大后会保留当前视野，直到 Reset Zoom。文件短暂被占用或正在替换时保留上一幅有效图，并继续重试。刷新 DAT 不会覆盖当前 `.plt` 显示设置。

## 编辑 SEQ

1. 在右侧双击所需命令。
2. 在弹窗中设置参数并确认。
3. 如果当前选中 Scan 开始行或对应 End Scan，新命令成为它的子指令。
4. 如果选中普通指令，新命令插入在其后且保持同级。
5. 双击已有指令可以重新编辑参数。
6. 使用“编辑”菜单移动、复制、粘贴或删除整个指令节点。

编辑 `Scan Temperature` 时，`Point definition` 可选：

- `Linear`：输入 Start、Stop 和 Points，程序生成包含首尾的等距温度点。
- `List`：在 `Temperature list` 输入英文逗号分隔点位，例如 `300, 250, 100, 20`。确认后显示为三位小数，按原顺序执行并允许重复点。

List 中任一空项、非数字或非有限值会在参数窗口中提示并阻止确认。运行前还会一次检查所有点是否满足配置文件温度上下限和速率；有一项越界就不会开始该 Scan。

设置数据文件有两种位置：`Run folder` 保存在本次自动运行目录；`Custom folder` 使用绝对路径。直接点击左侧 Data File 的 `Change` 并在保存窗口选择目录时，程序会自动使用 `Custom folder`，并把该目录作为本次会话下一次选择的起点。

删除或复制一个 Scan 会连同其所有子指令一起处理。

多行选择与普通 Windows 列表一致：按住 `Ctrl` 点击可逐行增减选择，按住 `Shift` 点击可连续选择。右键已选中的任意一行会保留整组选择；右键未选中行则只操作该行。

在任意命令行或 `End Scan` 上右击，可直接使用：

| 右键命令 | 键盘 | 结果 |
|---|---|---|
| `Disable` | `Ctrl+D` | 批量禁用所选命令；保存为行首 `F`，运行时跳过 |
| `Enable` | `Ctrl+E` | 批量恢复所选命令；保存为行首 `T` |
| `Delete` | `Delete` | 批量删除所选命令或完整 Scan 块 |
| `Copy` | `Ctrl+C` | 按文档顺序复制所选命令或完整 Scan 块 |
| `Paste` | `Ctrl+V` | 以当前焦点行为插入点，按原顺序粘贴独立副本 |

如果父 Scan 和其中的子行同时被选，Copy/Delete 只处理最外层 Scan 一次，防止内容重复；Scan 开始行和 `End Scan` 同时被选也只算一个节点。粘贴多项后，新建的各顶层副本保持选中，便于继续批量处理。

禁用 Scan 时，其子命令不会执行，但子命令自身的启用状态不会被覆盖；重新 Enable 该 Scan 后原结构立即恢复。灰色删除线表示该行不会在当前状态下运行。右键 `End Scan` 等价于选中对应 Scan；右键 `End Sequence` 时可把内容粘贴到末尾。

## 运行

1. 检查底部设备均已连接。
2. 加载或编辑 SEQ。
3. 点击 Run。
4. Pause 在安全检查点暂停执行；已发出的设备目标仍由设备执行。
5. Resume 从原嵌套位置继续。
6. Stop 取消当前和后续步骤，并按配置保持温度和磁场。

运行期间禁止编辑 SEQ 和更改数据文件，确保配置快照与实际执行内容一致。

## 手动控制

双击底部状态块：

- 温度/磁场：显示当前值，可输入目标、速率和模式，也可以保持当前值。默认温度以 K 显示三位小数，磁场以 Oe 显示两位小数。
- 测量设备：显示各通道，可执行一次手动测量。

手动设定也经过和 SEQ 相同的上下限及速率检查。

## Warning 和 Error

Warning：

- 显示“测量继续运行”。
- SEQ 不停止。
- 相同 `source + code + context` 在恢复前只弹出一次。
- 事件写入 `events.dat`。

Error：

- 显示“测量已中止”。
- 活动 SEQ 进入 Faulted。
- 温度、磁场执行中止保持策略。
- 后续 Measure 不会执行。

菜单 `Simulation` 可人工触发 Warning 和 Error，以验证实验流程。

## 输出目录

每次运行创建：

```text
runs/YYYYMMDD_HHMMSS_SequenceName/
  configuration.toml
  sequence.seq
  experiment.dat
  events.dat
```

左侧 View 按钮只显示独立 Data Browser，不会自动把它切换到最近运行文件。要查看实验输出，请将对应运行目录中的 DAT 拖入窗口或使用 `Open DAT`。

左侧 DAT/SEQ 名称采用中间省略以保持侧栏可缩放；鼠标悬停可查看完整路径或文件名。省略只影响显示，不改变 SEQ 中保存的路径。

如果旧 SEQ 指向另一台电脑的绝对路径且没有 `external` 标记，默认会把文件重定向到本次运行目录并产生一次 Warning。用户通过左侧 `Change` 选择的路径会带 `external` 标记，仅授权该条命令；设置 `allow_external_paths = true` 则会全局允许所有绝对路径。

## 配置修改

1. 关闭程序。
2. 备份 `configs/default.toml`。
3. 修改参数。
4. 运行自动测试或无界面演示。
5. 重新启动程序。

配置不热重载。正在运行时修改文件不会改变该次实验。

## 常见问题

### run.bat 提示未安装 PySide6

先运行 `setup.bat`。若下载失败，检查代理、网络和 Python 版本。

### SEQ 显示黄色行

该指令暂未识别。程序会保留文字，但运行时报告 Warning 并跳过。为它开发正式命令解析器前不要用于关键实验。

### 运行一直等待稳定

检查：

- 目标是否可达。
- `tolerance` 是否过小。
- `max_slope_per_minute` 是否小于真实噪声导致的斜率。
- `dwell_seconds` 是否过长。
- 当前读数是否持续更新。

超时后的行为由 `alarms.stability_timeout` 决定。

### DAT 中文在旧终端显示乱码

文件编码是 UTF-8。请用支持 UTF-8 的编辑器、Origin 导入设置或 Python 打开；不要根据旧 PowerShell 的显示结果判断文件已损坏。

### PLT 没有恢复

确认 `.plt` 与 DAT 位于同一目录且主文件名相同，例如 `sample.dat` 对应 `sample.plt`。如果列名已改变，浏览器会保留 DAT 的默认显示并提示该 PLT 不适用；修正列名后使用右键 `Reload Plot Format`。

## 关闭与恢复

关闭正在运行的程序会先询问确认。确认后：

1. 请求中止 SEQ。
2. 执行温度和磁场保持。
3. 停止轮询。
4. 断开设备。
5. 刷新并关闭日志文件。

当前版本不会从崩溃点自动恢复运行。崩溃后应检查设备实际状态和 `events.dat`，再从新 SEQ 开始。
