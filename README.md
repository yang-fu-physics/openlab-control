# OpenLab Control

OpenLab Control 是一个参考 Quantum Design MultiVu 操作方式、面向外部实验设备的
Python/PySide6 控制框架。它不控制 PPMS 本体。温控仪、磁体电源和只读监视设备由
Device Plugin 提供；吉时利组合表、Lakeshore 372 AC Bridge 等完整测量方案由独立的
Measurement Module 提供。

当前版本：`0.11.0`。核心框架、扩展 API 和仿真流程进入稳定版本；默认配置全部使用
仿真设备。Lake Shore 372A 等尚未完成真机验证的硬件扩展仍各自保持 Beta 状态。

![主窗口](docs/main-window-preview.png)

## 主要能力

- MultiVu 风格的浮动 SEQ 编辑器和可任意嵌套的 Temperature/Field/Time Scan。
- Pause、Stop、Call Sequence、Set Datafile、Remark 和无参数 `Measure`。
- 配置中的温场上下限与最大速率同时约束手动控制、SEQ 参数窗口和运行时执行。
- 每个设备实例、每个 Measurement Module 分别运行在独立子进程中。
- 温场主设备失联后进入可配置恢复窗；默认每 2 秒重试，1 分钟后转为故障。
- 一个温度/磁场种类最多一个主控设备；其他设备默认只读监视。
- 每次启动所有 Measurement Module 都是 Disabled；Enable 只初始化并加载设置，不会
  自动 Apply。
- 一个 `Measure` 并行等待所有 Enabled 模块；每个模块可以流式返回多行。
- Warning 继续运行且按 Source/Code/Context 去重；Error 中止 SEQ。
- 可选异步 HTTP 报警报告：Warning 仅测试员，Error 同时通知管理员和测试员；默认
  关闭且网络失败不阻塞 SEQ。
- Stop/Error 后温度和磁场保持当前状态；`2nd Stage` 仅显示。
- 独立 Data Browser 可打开任意 DAT 并追踪文件追加，不与当前 Run 强制绑定。

## 启动与测试

源码环境：

```text
setup.bat
run.bat
```

`setup.bat` 安装 `requirements-lock.txt` 中经过验证的精确版本。`run.bat` 始终运行当前
源码，不会误用 `dist/` 中的旧 EXE。

完整源码测试与无界面仿真：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe run.py --headless-demo --sequence examples\nested_scan.seq
```

Windows 文件夹发布包：

```text
build.bat
```

输出位于 `dist\OpenLabControl\`。配置、文档、扩展模板和可写数据目录只放在 EXE
旁边，不会在 `_internal/` 中重复一份。

## 扩展仓库与离线安装

核心仓库的 `modules/` 和 `device_plugins/` 默认为空。发布包内提供两个 Git-ready
模板：

- `plugin_templates/measurement-modules-repository/`：所有 Measurement Module 共用
  的仓库模板，含硬件无关的 `simulated_transport`。
- `plugin_templates/device-plugins-private-repository/`：所有正式 Device Plugin 共用的
  私密仓库模板，含 fail-closed 控制器和只读 Monitor 骨架。

首阶段不提供在线商店。安装时手动复制一个完整扩展目录：

```text
plugin repository/modules/<id>/  -> OpenLabControl/modules/<id>/
plugin repository/plugins/<id>/  -> OpenLabControl/device_plugins/<id>/
```

重启后，程序会在首次加载时显示类型、ID、版本、路径和内容指纹，必须由用户确认信任。
任何源码或 wheel 改动都会使旧信任失效。

声明第三方依赖的扩展必须携带：

- 精确 `==` 版本和 SHA-256 的 `requirements.lock`；
- 与目标 Windows/Python 匹配的本地 wheels（扩展自己的 `wheels/` 或应用共享
  `wheels/`）。

程序只执行 `--no-index --require-hashes` 离线安装，不存在联网回退。每个扩展的依赖
安装到 `plugin_runtime/<type>/<id>/<fingerprint>/site-packages/`，不会进入主进程，也
不会与其他扩展共享版本。

## 第一次使用示例模块

1. 把
   `plugin_templates/measurement-modules-repository/modules/simulated_transport`
   完整复制到 `modules/simulated_transport`。
2. 重启程序并打开 `Modules`。
3. 勾选 `Simulated Transport`，核对首次信任提示后确认。
4. 在默认 `Settings` 页检查参数；如需发送设置，点击 `Apply Settings` 并再次确认。
5. 运行含无参数 `T Measure` 的 SEQ。
6. Disable 成功后模块才会 abort、关闭工作进程并隐藏窗口。

## 更换温度或磁场设备

正式设备代码放在私密 Device Plugin 仓库中，不使用“每个设备一个核心分支”。部署时：

1. 把目标插件目录复制到 `device_plugins/`。
2. 只修改 `configs/default.toml` 中相应设备的 `plugin = "<plugin-id>"` 及该设备的地址、
   安全上下限、速率和超时。
3. 重启并确认插件信任。

协议连接、读取、设定和 Hold 由插件实现；主配置仍是安全限制的权威来源。仪表自身的
面板设置暂不由 OpenLab Control 自动配置。

## 目录

```text
configs/                 主配置；选择设备插件和安全限制
modules/                 手动安装的 Measurement Modules（默认空）
device_plugins/          手动安装的 Device Plugins（默认空）
module_data/<id>/        模块保存设置
plugin_runtime/          各扩展隔离且可重建的离线依赖
plugin_state/            本机扩展信任记录
wheels/                  可选共享离线 wheels
plugin_templates/        两个独立扩展仓库模板
integrations/            NoneBot 报警接收器等外部集成参考
examples/                 SEQ/DAT/PLT 示例
runs/                     每次 Run 的数据、日志和快照
docs/                     操作、格式、架构和测试文档
src/labcontrol/           核心源码
tests/                    核心自动测试
```

## 文档

- [操作手册](docs/OPERATIONS.md)
- [SEQ 格式](docs/SEQUENCE_FORMAT.md)
- [DAT 与事件格式](docs/DAT_FORMAT.md)
- [配置参考](docs/CONFIGURATION.md)
- [系统架构](docs/ARCHITECTURE.md)
- [插件与模块开发](docs/PLUGIN_DEVELOPMENT.md)
- [技术规格](docs/TECHNICAL_SPECIFICATION.md)
- [测试计划](docs/TEST_PLAN.md)
- [验证报告](docs/VERIFICATION_REPORT.md)
- [Beta 2 历史验证报告](docs/VERIFICATION_REPORT_BETA2.md)
- [Beta 1 历史验证报告](docs/VERIFICATION_REPORT_BETA1.md)
- [架构决策](docs/DECISIONS.md)

## 真实仪表安全门槛

当前版本没有真实硬件验证，不应直接用于无人值守实验。接入真实仪表前必须完成只读
身份确认、有限通信超时、低风险写入、上下限三层校验、Hold 新鲜读回、写超时不重放、
失联恢复、Stop/Error、进程强杀和硬件互锁测试。软件进程隔离不能替代仪表自身的安全
状态、限流、限压、限温、磁体保护或人工急停。
