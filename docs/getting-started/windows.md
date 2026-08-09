# 使用 Windows 发布包

Windows 文件夹发布包已经包含 Python、PySide6、PyVISA 和框架统一依赖，不需要另装
Python。它仍需要与你的 GPIB/VISA 硬件匹配的厂商 VISA Runtime，例如 NI-VISA。

## 下载与校验

当前稳定版本为 `v0.13.0`：

- [下载 Windows x64 ZIP](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.13.0/OpenLabControl-v0.13.0-windows-x64.zip)
- [下载 SHA-256 文件](https://github.com/yang-fu-physics/openlab-control/releases/download/v0.13.0/OpenLabControl-v0.13.0-windows-x64.zip.sha256)

在 PowerShell 中校验：

```powershell
Get-FileHash .\OpenLabControl-v0.13.0-windows-x64.zip -Algorithm SHA256
Get-Content .\OpenLabControl-v0.13.0-windows-x64.zip.sha256
```

两处哈希必须完全一致。不要直接从 ZIP 内运行程序；先完整解压到普通可写目录。

## 第一次启动

1. 双击 `OpenLabControl.exe`。
2. 确认底部显示仿真 Temperature、Magnetic Field 和 2nd Stage。
3. 打开 **Modules**，确认所有模块均未勾选。
4. 打开 `examples/nested_scan.seq`。
5. 点击 Run，完成后检查 `runs/<时间>_nested_scan/`。

!!! note "VISA 资源发现"

    PyVISA 是框架共享依赖，但它不是 GPIB 硬件驱动。若提示找不到 VISA implementation，
    请安装仪表接口厂商的 VISA Runtime；不要在模块中重复安装另一套 PyVISA。

## 手动安装示例模块

把一个完整目录复制到程序旁边的 `modules/`：

```text
plugin_templates/measurement-modules-repository/modules/simulated_transport/
    ↓
modules/simulated_transport/
```

重启后打开 Modules，勾选模块并核对首次信任提示。Enable 会初始化并打开独立窗口；它会
读取保存的界面值，但不会自动 Apply。

## Windows 包不会改变的规则

- 模块和设备实例仍各自在独立子进程运行。
- Warning 继续 SEQ，Error 中止 SEQ。
- Load SEQ 只导入模块界面值，不自动 Enable、连接或 Apply。
- Data Browser 可打开任意 DAT，不强制绑定当前 Run。
- 打包版与源码版使用同一解析器、安全限制和测试语义。
