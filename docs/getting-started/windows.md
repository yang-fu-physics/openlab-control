# 使用 Windows 发布包

Windows 发布包已内置 Python 及所有必需依赖（PySide6、PyVISA 等），**解压就能运行，不需要在实验电脑上安装 Python**。

- 如果只跑仿真练习，不需要装任何驱动；
- 如果要连接真实的 GPIB / USB / 串口 VISA 仪表，需先在本机安装对应硬件的 VISA 驱动（最常见的是 [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html) 或 Keysight IO Libraries）。

## 1. 下载与完整性校验

当前稳定版本为 `v0.20.6`：

- [下载 Windows x64 ZIP](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.20.6/OpenLabControl-v0.20.6-windows-x64.zip)
- [下载 SHA-256 校验文件](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.20.6/OpenLabControl-v0.20.6-windows-x64.zip.sha256)

下载后可在 PowerShell 中核对哈希值（两行结果一致即完整无损）：

```powershell
Get-FileHash .\OpenLabControl-v0.20.6-windows-x64.zip -Algorithm SHA256
Get-Content .\OpenLabControl-v0.20.6-windows-x64.zip.sha256
```

将 ZIP **完整解压**到一个普通可写目录（例如 `D:\OpenLabControl\`），不要在压缩包里直接双击运行。

!!! tip "保持目录完整"

    根目录下的 `OpenLabControl.exe` 与 `InstrumentScanner.exe` 共用同一个 `_internal/` 文件夹，
    同时程序会直接读写同级的 `configs/`、`modules/`、`system_instruments/`、`module_data/` 和 `runs/`。
    不要单独把某个 EXE 拖到桌面，如需快捷方式请右键创建快捷方式。

## 2. 首次启动与启用仿真

1. **体验空启动**：双击 `OpenLabControl.exe`。全新安装不带任何系统仪表面板，也能正常打开界面。
2. **打开仪表扫描器**：关闭主程序，双击同目录的 **`InstrumentScanner.exe`**。
   - **第 1 页（VISA Resources）**：扫描本机 VISA 仪表，未分配给系统仪表的地址会保存到 `configs/visa.resources.toml` 供测量模块使用。
   - **中间页（各系统仪表）**：如果已安装真实系统仪表驱动，在这里添加实例并填写连接信息（网口仪表的 Host/Port 也在对应型号页面填写）。
   - **最后一页（Review & Save）**：这里有三个内置仿真选项（默认关闭）。首次练习时可勾选它们，检查面板顺序与写入预览后点击保存。
3. **重新打开主程序**：双击 `OpenLabControl.exe`，底部就会出现对应的仪表面板。

更详细的向导说明见 [扫描与配置仪表](../guides/instrument-scanner.md)。

## 3. 试用自带的示例测量模块

解压后的 `modules/` 目录里已经自带两个不依赖真实硬件的示例模块：

- **`Simulated Transport`**：最简四通道模拟电阻模块，没有复杂设置，适合熟悉 Enable / Measure / Disable 流程。
- **`Tutorial Resistance`**：带设置页、原始数据和自定义 SEQ 指令的完整教学模块。

操作方式：点击工具栏 **Modules** → 勾选模块 → 窗口自动弹出。注意：**勾选 Enable 只是加载模块并恢复上次界面上的参数，不会自动把参数发给仪表**；如果模块有 Settings 页，检查无误后点击 **Apply Settings** 才会真正下发。

## 4. 调整界面缩放与字体（高分屏适配）

如果 4K 屏幕下文字过小或窗口太挤，点击顶部菜单 **View → Appearance**：

- **Overall size**：整体缩放（自动或 75%–200%）；
- **Text size**：在整体缩放基础上独立调整字号（70%–150%）；
- **At startup**：选择记住上次窗口位置、始终最大化或恢复默认布局。

点击 **Save** 并重启软件后生效（所有测量模块窗口会自动继承同一套字号）。该设置保存在 Windows 当前用户目录，不会改动项目里的 `configs/`、SEQ 或数据文件。

## 5. 准备接入真实仪表前

平时只需维护 `configs/general.toml`；扫描器生成的 `configs/visa.resources.toml`、`configs/instruments/` 含有本机地址，不要提交到公开仓库。已有 `configs/pid/` 文件在重新保存扫描器配置时不会被覆盖或删除。

!!! danger "接入真实硬件前请先核对"

    复制驱动或改完地址后，请先阅读该型号的 README 并完成 [真实仪表安全清单](../guides/safety-checklist.md)，
    从只读查询与低风险测试负载开始，切勿跳过现场检查直接无人值守运行。
