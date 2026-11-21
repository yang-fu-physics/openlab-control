# 扫描与配置仪表（Instrument Scanner）

**Instrument Scanner** 是一个独立的图形配置向导，用来做两件事：

1. 扫描电脑当前连着哪些 GPIB / USB / 串口 VISA 仪表，决定哪些留给 **Measurement Module**；
2. 配置每台 **System Instrument**（温控仪、磁体电源、压缩机等）的地址、面板开关、排列顺序与安全上下限。

它不会改动通用配置 `configs/general.toml`，只负责管理以下三个位置：

```text
configs/
├─ visa.resources.toml              # 留给测量模块的 VISA 地址清单
├─ instruments/<仪表ID>.toml        # 各系统仪表的实例、面板与安全限值
└─ pid/<实例ID>.toml                # 温控实例独立的现场 PID 文件
```

> 全新安装不需要先建这些文件。没有配置任何系统仪表时，OpenLab Control 也能正常启动。

## 1. 打开扫描器

- **Windows 发布包**：关闭主程序后，直接双击根目录的 **`InstrumentScanner.exe`**（与 `OpenLabControl.exe` 共用 `_internal/`，不要单独移动）。
- **源码环境**：
  ```powershell
  .\.venv\Scripts\python.exe .\tools\instrument_scanner.py
  ```

如果点击扫描时提示找不到 VISA implementation，说明电脑还缺底层硬件驱动。点击弹窗里的 [NI-VISA 官方下载链接](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)（或安装 Keysight IO Libraries）装好后重新打开扫描器即可。

## 2. 三步完成配置

向导左侧按固定顺序分成三个阶段：

### 第一步：VISA Resources（扫描并分配地址）

点击 **Scan VISA Resources** 后，扫描器会对每个检测到的 VISA 地址用有限超时发一次 `*IDN?` 并立刻断开，**不会**发复位、改量程或开输出命令。没有响应 `*IDN?` 的老仪表仍会列出地址，需要你结合接线与仪表面板手动辨认。

每条扫描结果以一张卡片显示：

- **Keep for Measurement Module**：新扫描到的地址默认勾选（留给测量模块使用），建议把 **Resource ID** 改成易读且长期稳定的名字，例如 `keithley_2400_1`（小写字母开头，只含小写字母、数字和下划线）。
- **曾保存但本次未检测到的地址**：会灰显为 **Not detected** 并默认继续保留，避免临时断电丢配置；只有取消勾选才会删掉。
- **地址互斥**：一旦某个 VISA 地址在后续页面被分配给了某个 System Instrument，这里会自动取消勾选并禁用，确保同一物理口不会被两类程序同时打开。

!!! warning "`*IDN?` 只用于方便认表"

    扫描器记录的身份字符串只是人工核对线索，不能代替驱动连接后的正式型号校验与现场安全边界确认。

### 第二步：逐页配置 System Instrument

`system_instruments/` 下每安装一种驱动，向导左侧就会多出一页（扫描器只读清单 `instrument.toml`，不运行驱动代码）：

1. 点击 **Add Instrument**，填写唯一的 **Instance ID**（同型号有多台物理仪表时可多次添加，每台都会有独立子进程和连接）。
2. **连接方式**：
   - 带 VISA 识别规则的仪表：在 **VISA Resource** 下拉框里选择本次扫描到的地址（会显示完整 `*IDN?` 并标出 `identity match`）。
   - 专用网络仪表（例如 ZLAN Modbus TCP）：不走 VISA 扫描，直接在该页填写模板声明的 `host`、`port` 等连接字段。
3. **型号专用参数**：按该型号 README 核对超时、通道映射、动作步骤等字段（请不要直接照搬清单默认值当成现场验证值）。
   - 可编辑的秒级时间和判稳时间统一保留 1 位小数（滤波字段除外）；若旧文件含超出该精度的数值，扫描器会明确报错提示修正，不悄悄舍入。
   - 系统仪表的单次框架操作总上限为 30 秒；若某项 I/O 超时设置过大导致整步预算超标，启动时会报配置错误 `INSTRUMENT_IO_TIMEOUT_TOO_HIGH`（不会瞎重连），调小对应超时即可。
4. **固定面板与角色**：驱动清单里定义好了可用的固定面板（`controller` / `readout` / `readout_grid` / `switch`），你可以在此开启、关闭并指定角色：
   - `sample_temp`：作为标准温度 SEQ（`Set / Scan Temperature`）的主控面板，**全局最多 1 个**；
   - `field`：作为标准磁场 SEQ（`Set / Scan Field`）的主控面板，**全局最多 1 个**；
   - `none`：只显示、记录、手动控制或提供按钮，不接管标准温场 SEQ，可任意多个。
   - 开启 `controller` 面板时，还必须核对主读数、上下限、默认/最大速率以及判稳参数。

!!! note "PID 文件怎么管理？"

    如果某款温控仪声明了 `pid_file` 字段，**第一次保存**该实例时，扫描器会把示例模板（或你通过 **Choose PID File…** 选择的已验证文件）复制到 `configs/pid/<实例ID>.toml`。
    文件生成后，以后再保存或移除该实例，扫描器都**不会覆盖或删除**这个 PID 文件。
    如果示例文件里故意留了空的 `zones = []`（如 Cryo-con 22C/24C），必须先填入本机验证过的 PID 区间再启动主程序。

### 第三步：Review & Save（仿真选项、排序与预览保存）

1. **可选内置仿真**：在这里按需勾选 **Simulated Temperature**、**Simulated Magnetic Field**、**Simulated 2nd Stage**（默认全部关闭）。注意：温度仿真会自动占用 `sample_temp`，磁场仿真会占用 `field`，不能与已有的同角色真实温控/磁体同时启用。
2. **全局面板排序**：使用 **Move Up / Move Down** 调整底部所有已启用面板的显示顺序。
3. **核对写入预览并保存**：预览区会明确标出每个文件的动作（`CREATE` / `OVERWRITE` / `UNCHANGED` / `DELETE` / `CREATE PID`）以及完整文本内容。

!!! danger "保存是全量生效，请务必看清预览"

    点击保存不是“局部追加”，而是按当前向导里的全部内容原子重写 `visa.resources.toml` 与 `configs/instruments/*.toml`，并删除预览中标为 `DELETE` 的旧文件（**现有 `configs/pid/*.toml` 始终保留**）。保存前请务必核对完整预览，确认没有把暂时离线但仍需保留的仪表误删。

保存完成后，重启 OpenLab Control 即可生效。主程序启动时会再次校验所有 ID、地址唯一性、角色和数值范围，有任何冲突都会在连接真实硬件前拦截。

## 附录：测量模块如何使用扫描结果？

测量模块不需要自己扫 VISA，直接从核心读取未分配的测量资源清单即可：

```python
for resource_id, info in api.resources().items():
    combo.addItem(f"{resource_id} — {info['identity']}", resource_id)
```

模块界面只保存稳定的 `resource_id`，后台连接时再用 `api.resource_address(resource_id)` 解析真实地址。以后电脑换口或改 GPIB 地址，只需在扫描器里更新一次，所有模块无需重填。
