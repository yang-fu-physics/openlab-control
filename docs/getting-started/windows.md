# 使用 Windows 发布包

Windows 文件夹发布包已经包含 Python、PySide6、PyVISA 和框架统一依赖，不需要另装
Python。纯仿真不需要 VISA Runtime；只有连接 GPIB/USB/VISA 硬件时，才需要安装与接口
匹配的厂商 Runtime，例如 NI-VISA。

## 下载与校验

当前稳定版本为 `v0.15.2`：

- [下载 Windows x64 ZIP](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.15.2/OpenLabControl-v0.15.2-windows-x64.zip)
- [下载 SHA-256 文件](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.15.2/OpenLabControl-v0.15.2-windows-x64.zip.sha256)

在 PowerShell 中校验：

```powershell
Get-FileHash .\OpenLabControl-v0.15.2-windows-x64.zip -Algorithm SHA256
Get-Content .\OpenLabControl-v0.15.2-windows-x64.zip.sha256
```

两处哈希必须完全一致。不要直接从 ZIP 内运行程序；先完整解压到普通可写目录。

## 第一次启动

1. 双击 `OpenLabControl.exe`。需要确认仪表地址时，双击同目录的
   `InstrumentScanner.exe`。两个程序共享同一个 `_internal`，不要单独移动任一 EXE。
2. 确认底部显示仿真 Temperature、Magnetic Field 和 2nd Stage。
3. 打开 **Modules**。刚解压时列表为空是正常现象，因为发布包不会预装测量模块。
4. 保持列表为空，打开 `examples/nested_scan.seq`。
5. 点击 Run，完成后检查 `runs/<时间>_nested_scan/`。无模块 Warning 是预期结果。
6. 再按下一节安装 `simulated_transport`，练习 Enable/Disable 和模块数据写入。

## 调整字号和窗口大小

打开 **View → Appearance**。整体界面可选自动或 75%–200%，文字还可独立选择
70%–150%；例如按钮保持 100%，文字选择 80%。窗口启动方式可以选择记住上次位置、始终
最大化或恢复默认布局。

点击 Save 后关闭并重新打开程序。这个设置只保存在当前 Windows 用户目录，不会修改
`configs/*.toml`、仪表安全上下限、SEQ 或 DAT。模块窗口也会自动继承同一字号，不需要在
每个模块中重复设置。

!!! note "VISA 资源发现"

    PyVISA 是框架共享依赖，但它不是 GPIB 硬件驱动。若提示找不到 VISA implementation，
    请安装仪表接口厂商的 VISA Runtime；不要在模块中重复安装另一套 PyVISA。

## 手动安装示例模块

把一个完整目录复制到程序旁边的 `modules/`：

```text
templates/measurement-modules-repository/modules/simulated_transport/
    ↓
modules/simulated_transport/
```

重启后打开 Modules，勾选模块并核对首次信任提示。Enable 会初始化并打开独立窗口；它会
读取保存的界面值，但不会自动 Apply。

这里的模板来自刚刚校验过 SHA-256 的发布 ZIP；弹窗指纹用于建立这台电脑上的首次信任
基线。单独下载第三方模块时，应与作者发布的摘要或签名比较。

`simulated_transport` 用于体验最小 Enable/Measure/Disable 流程，没有自定义设置。开发者
教程中的 `tutorial_resistance` 是另一个示例，用来学习设置窗口、四行结果和模块附加功能。

需要现场配置时，把 `configs/default.toml` 复制为 `configs/site.local.toml`，然后用
`OpenLabControl.exe --config configs\site.local.toml` 启动。不要把含真实地址的配置上传到
公开仓库。

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
