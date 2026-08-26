# 使用 Windows 发布包

Windows 文件夹发布包已经包含 Python、PySide6、PyVISA 和框架统一依赖，不需要另装
Python。纯仿真不需要 VISA Runtime；只有连接 GPIB/USB/VISA 硬件时，才需要安装与接口
匹配的厂商 Runtime，例如 NI-VISA。

## 下载与校验

当前稳定版本为 `v0.19.0`：

- [下载 Windows x64 ZIP](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.19.0/OpenLabControl-v0.19.0-windows-x64.zip)
- [下载 SHA-256 文件](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.19.0/OpenLabControl-v0.19.0-windows-x64.zip.sha256)

在 PowerShell 中校验：

```powershell
Get-FileHash .\OpenLabControl-v0.19.0-windows-x64.zip -Algorithm SHA256
Get-Content .\OpenLabControl-v0.19.0-windows-x64.zip.sha256
```

两处哈希必须完全一致。不要直接从 ZIP 内运行程序；先完整解压到普通可写目录。

## 第一次启动

1. 双击 `OpenLabControl.exe`。程序读取 `configs/general.toml`；全新安装没有 System 面板也能
   正常打开。
2. 需要确认仪表或启用仿真时，关闭主程序，再双击同目录的 `InstrumentScanner.exe`。两个
  程序共享一个 `_internal`，不要单独移动任一 EXE。
3. 扫描器第一页只处理 VISA。未分配地址保存到 `configs/visa.resources.toml`，供
   Measurement Module 使用；分配给 System Instrument 的地址只写入对应实例。
4. 后续每页配置一种已安装的 System Instrument。专用网络仪表的 Host/Port 由自己的模板
   字段提供。最后一页可以选择三个仿真，它们默认都不勾选。
5. 检查完整写入预览和面板顺序后保存，再启动 `OpenLabControl.exe`。
6. 打开 **Modules**，按下一节使用示例模块练习 Enable/Disable 和数据写入。

## 调整字号和窗口大小

打开 **View → Appearance**。整体界面可选自动或 75%–200%，文字还可独立选择
70%–150%；例如按钮保持 100%，文字选择 80%。窗口启动方式可以选择记住上次位置、始终
最大化或恢复默认布局。

点击 Save 后关闭并重新打开程序。这个设置只保存在当前 Windows 用户目录，不会修改
`configs/*.toml`、仪表安全上下限、SEQ 或 DAT。模块窗口也会自动继承同一字号，不需要在
每个模块中重复设置。

!!! note "VISA 资源发现"

    PyVISA 是框架共享依赖，但它不是 GPIB 硬件驱动。若提示找不到 VISA implementation，
    可直接点击扫描失败弹窗中的 [NI-VISA 官方下载链接](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)，
    或安装仪表接口厂商提供的其他 VISA Runtime。安装后重新打开扫描器；不要在模块中重复
    安装另一套 PyVISA。完整选择步骤见[扫描与配置仪表](../guides/instrument-scanner.md)。

## 运行示例模块

`simulated_transport` 与 `tutorial_resistance` 已放在程序旁的 `modules/`。打开 Modules
并勾选需要的模块；Enable 会初始化并打开独立窗口，它会
读取保存的界面值，但不会自动 Apply。

`simulated_transport` 用于体验最小 Enable/Measure/Disable 流程，没有自定义设置。开发者
教程中的 `tutorial_resistance` 是另一个示例，用来学习设置窗口、四行结果和模块附加功能。

现场只需维护 `configs/general.toml`；仪表地址和面板选择由 Instrument Scanner 写入
`configs/visa.resources.toml` 与 `configs/instruments/`。不要把含真实地址的生成配置上传到
公开仓库。扫描器每次保存都会完整覆盖最终预览中的生成文件；现有 `configs/pid/` 文件不会
被覆盖或删除。

!!! danger "改地址不等于可以控制真机"

    先取得与实际型号、固件和接线匹配的 [System Instrument](../development/system-instrument.md)，
    审查其命令与安全行为，并完成 [仪表安全清单](../guides/safety-checklist.md)。之后才进行
    有人在场的低风险真机测试；System Instrument 示例不能直接用于任意仪表。

## Windows 包不会改变的规则

- 模块和仪表实例仍各自在独立子进程运行。
- Warning 继续 SEQ，Error 中止 SEQ。
- Load SEQ 只导入模块界面值，不自动 Enable、连接或 Apply。
- Data Browser 可打开任意 DAT，不强制绑定当前 Run。
- 打包版与源码版使用同一解析器、安全限制和测试语义。
