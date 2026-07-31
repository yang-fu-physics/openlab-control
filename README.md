# OpenLab Control

OpenLab Control 是一个参考 Quantum Design MultiVu 操作方式、面向外部实验设备的
Python/PySide6 控制框架。它不控制 PPMS 本体。温控仪、磁体电源和只读监视设备由
Device Plugin 提供；吉时利组合表、Lakeshore 372 AC Bridge 等完整测量方案由独立的
Measurement Module 提供。

当前版本：`0.11.5`。核心框架、扩展 API 和仿真流程为稳定版本；默认配置全部使用
仿真设备。Lake Shore 372A、LR-700、Keithley 6221/2182A/7001/3706A 等尚未完成
真机验证的硬件扩展仍各自保持 Beta 状态。

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
- 一个 `T Measure` 按扫描模块的逻辑槽位并集展开，每个通道槽位写一行；同一槽位的
  模块并行测量并合入该行，单次模块在每个槽位重新测量。
- Measurement Module 必须声明 `once_per_slot` 或 `aligned_slots`；缺失时界面提示并按
  `once_per_slot` 兼容执行。
- 模块可为正式结果行附带有限原始采样序列，由中央写入独立无表头 `rawdata` sidecar。
- Warning 继续运行且按 Source/Code/Context 去重；Error 中止 SEQ。
- 可选异步 HTTP 报警报告：Warning 仅测试员，Error 同时通知管理员和测试员；默认
  关闭且网络失败不阻塞 SEQ。
- Stop/Error 后温度和磁场保持当前状态；`2nd Stage` 仅显示。
- 独立 Data Browser 可打开任意 DAT 并追踪文件追加，不与当前 Run 强制绑定；已知绝对
  时间戳显示为实际日期时间，数值轴使用 `1/2/5 × 10ⁿ` 整齐主刻度。
- 每次 Run 独立保存节流后的设备状态宽表，包含当前值、目标、速率、稳定与连接状态。
- Live Trend 使用设备采样时间并合并 GUI 重绘，不改变设备轮询或 DAT 采样频率。

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

## 扩展示例与离线安装

核心仓库的 `modules/` 和 `device_plugins/` 默认为空。发布包内提供 Git-ready
扩展示例：

- `plugin_templates/measurement-modules-repository/`：所有 Measurement Module 共用
  的仓库模板，含硬件无关的 `simulated_transport`。
- Device Plugin 示例：温控、磁场和 Monitor 的协议、安全行为及界面会随具体设备
  改变，因此核心只提供 fail-closed 控制器和只读 Monitor 骨架，不宣称这些示例可
  直接控制任意真实仪表。

首阶段不提供在线商店。安装时手动复制一个完整扩展目录：

```text
plugin repository/modules/<id>/  -> OpenLabControl/modules/<id>/
plugin repository/plugins/<id>/  -> OpenLabControl/device_plugins/<id>/
```

重启后，程序会在首次加载时显示类型、ID、版本、路径和内容指纹，必须由用户确认信任。
任何源码或 wheel 改动都会使旧信任失效。

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 是主框架统一提供并锁定
版本的依赖。模块和设备插件直接使用这同一组版本，不需要各自复制 wheel 或准备
`plugin_runtime`。manifest 可以声明兼容范围；若范围不接受框架版本，程序会在导入
扩展源码前拒绝加载。

只有声明框架尚未提供的额外第三方依赖时，扩展才需要携带：

- 精确 `==` 版本和 SHA-256 的 `requirements.lock`；
- 与目标 Windows/Python 匹配的本地 wheels（扩展自己的 `wheels/` 或应用共享
  `wheels/`）。

额外依赖只执行 `--no-index --require-hashes` 离线安装，不存在联网回退，并安装到
`plugin_runtime/<type>/<id>/<fingerprint>/site-packages/`。只有这些额外依赖加入对应
worker；不会污染主进程或覆盖框架统一版本。

## 第一次使用示例模块

1. 把
   `plugin_templates/measurement-modules-repository/modules/simulated_transport`
   完整复制到 `modules/simulated_transport`。
2. 重启程序并打开 `Modules`。
3. 勾选 `Simulated Transport`，核对首次信任提示后确认。
4. 在默认 `Settings` 页检查参数；如需发送设置，点击 `Apply Settings` 并再次确认。
5. 运行含无参数 `T Measure` 的 SEQ。
6. Disable 成功后模块才会 abort、关闭工作进程并隐藏窗口。

保存一个 SEQ 时，程序会把该实验已关联模块和当前 Enabled 模块的界面值保存为同目录
同名伴随文件，例如 `experiment.seq` + `experiment.modules.toml`。以后 Load
`experiment.seq` 会同时导入这些设置。导入过程不自动 Enable、不连接仪表，也不
Apply；模块仍遵守“启动默认 Disabled，用户核对后显式 Apply”的安全规则。没有伴随文件
的旧 SEQ 保持原行为。

## 更换温度或磁场设备

不同温控器、磁体电源和 Monitor 使用各自的 Device Plugin，不需要为设备维护核心
分支。核心仓库只提供接口和 fail-closed 示例。部署实际插件时：

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
plugin_runtime/          仅存放扩展额外依赖的隔离、可重建 runtime
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
- [0.11.3 历史验证报告](docs/VERIFICATION_REPORT_0_11_3.md)
- [Beta 2 历史验证报告](docs/VERIFICATION_REPORT_BETA2.md)
- [Beta 1 历史验证报告](docs/VERIFICATION_REPORT_BETA1.md)
- [架构决策](docs/DECISIONS.md)

## 真实仪表安全门槛

当前版本没有真实硬件验证，不应直接用于无人值守实验。接入真实仪表前必须完成只读
身份确认、有限通信超时、低风险写入、上下限三层校验、Hold 新鲜读回、写超时不重放、
失联恢复、Stop/Error、进程强杀和硬件互锁测试。软件进程隔离不能替代仪表自身的安全
状态、限流、限压、限温、磁体保护或人工急停。
