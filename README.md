# OpenLab Control

OpenLab Control 是一个参考 Quantum Design MultiVu 操作方式、面向外部实验设备的
Python/PySide6 控制框架。它不控制 PPMS 本体。温控仪、磁体电源和只读监视设备由
Device Plugin 提供；吉时利组合表、Lakeshore 372 AC Bridge 等完整测量方案由独立的
Measurement Module 提供。

当前稳定版本：`0.14.0`。该版本把前面板的一秒常规轮询与测量即时采样分开，并让同一
Device Plugin 连接返回辅助温度、加热输出、量程和仪表稳定状态；默认配置仍全部使用仿真
设备。Cryo-con 22C/24C、Lake Shore 372A、LR-700、Keithley 6221/2182A/7001/3706A 等
尚未完成真机验证的硬件扩展仍各自保持 Beta 状态。

开发者网站：<https://yang-fu-physics.github.io/openlab-control/>。网站提供可搜索的中文
快速开始、Measurement Module、Device Plugin、测试、安全清单和公共 API 教程。目前只
发布稳定版文档，不显示开发版入口。

![主窗口](docs/main-window-preview.png)

截图为 v0.13.0；已安装一个示例模块，但仍保持 Disabled。

## 主要能力

- MultiVu 风格的浮动 SEQ 编辑器和可任意嵌套的 Temperature/Field/Time/模块自定义 Scan。
- Pause、Stop、Call Sequence、Set Datafile、Remark 和无参数 `Measure`。
- 配置中的温场上下限与最大速率同时约束手动控制、SEQ 参数窗口和运行时执行。
- 每个设备实例、每个 Measurement Module 分别运行在独立子进程中。
- 温场主设备失联后进入可配置恢复窗；默认每 2 秒重试，1 分钟后转为故障。
- 一个温度/磁场种类最多一个主控设备；其他设备默认只读监视。
- 每次启动所有 Measurement Module 都是 Disabled；Enable 只初始化并加载设置，不会
  自动 Apply。
- Enabled 模块会在左侧 `Sequence Status` 下方显示紧凑监视卡；卡片可显示运行状态、
  Warning/Error、窗口是否最小化和最近一次测量值，点击即可恢复模块窗口。这里仅使用
  模块已经返回的结果缓存，不会额外读取仪表。
- 一个 `T Measure` 按模块可选的 `slots` 并集展开，每个通道槽位写一行；同一槽位的
  模块并行，未声明 `slots` 的模块跟随每个槽位测量。
- Measurement Module 最小只需 `open(api)`、`measure(slot, api)`、`close(api)`；会维持
  输出的模块还要用 `on_event` 在每次 `run_end` 完成明确收尾。默认关闭输出；确有连续
  偏置需求的模块可提供默认关闭的保留选项，并在保留前读回确认。按需增加 `configure`、
  `slots` 和自己的 SEQ 指令，不需要修改核心解析器。
- 模块可为正式结果行附带有限原始采样序列，由中央写入独立无表头 `rawdata` sidecar。
- Warning 继续运行且按 Source/Code/Context 去重；Error 中止 SEQ。
- 可选异步 HTTP 报警报告：Warning 仅测试员，Error 同时通知管理员和测试员；默认
  关闭且网络失败不阻塞 SEQ。
- Stop/Error 后温度和磁场保持当前状态；`2nd Stage` 仅显示。
- 独立 Data Browser 可打开任意 DAT 并追踪文件追加，不与当前 Run 强制绑定；已知绝对
  时间戳显示为实际日期时间，数值轴使用 `1/2/5 × 10ⁿ` 整齐主刻度。
- 每次 Run 独立保存节流后的设备状态宽表，包含当前值、目标、速率、稳定、连接状态以及
  插件随同一连接返回的辅助温度、加热输出、量程等固定附加列。
- Live Trend 使用设备采样时间并合并 GUI 重绘，不改变设备轮询或 DAT 采样频率。
- Measurement Module 需要温场值时会即时采样，不复用最多一个前面板刷新周期以前的缓存；
  同一时刻多个模块请求会合并。
- **View → Appearance** 可分别调整 75%–200% 的整体界面和 70%–150% 的文字大小，
  并选择记住窗口尺寸、始终最大化或使用默认布局；保存后重启生效。外观值只属于当前
  Windows 用户，不写入实验配置、SEQ 或运行快照。

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
版本的依赖。模块和设备插件直接使用这同一组版本，不在各自清单重复声明，也不需要
复制 wheel 或准备 `plugin_runtime`。

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
4. 该最小示例没有自定义设置，通用 `Settings` 页无需 Apply。
5. Enable 后，左侧 `Sequence Status` 下方会出现模块卡片；最小化独立窗口后可点击卡片
   恢复。
6. 运行含无参数 `T Measure` 的 SEQ；卡片会显示本轮 R1–R4 的最近结果。
7. Disable 时核心调用模块 `close(api)`，随后关闭工作进程、隐藏窗口并移除卡片。

保存一个 SEQ 时，程序会把该实验已关联模块和当前 Enabled 模块的界面值保存为同目录
同名伴随文件，例如 `experiment.seq` + `experiment.modules.toml`。以后 Load
`experiment.seq` 会同时导入这些设置。导入过程不自动 Enable、不连接仪表，也不
Apply；模块仍遵守“启动默认 Disabled，用户核对后显式 Apply”的安全规则。没有伴随文件
的旧 SEQ 保持原行为。

模块可以在 `backend.py` 中声明可选 `sequence_commands`。只有模块完成 Enable 后，右侧
`Sequence Command Bar` 才会直接出现以该模块名称为标题的指令组；Disable 或 worker
失效时立即移除。普通模块指令只改变该模块的运行状态，模块扫描会逐点调用后端并在每点
成功后执行其嵌套子命令。它们本身不写 DAT；需要记录测量结果时仍显式插入 `Measure`。
包含未安装、Disabled 或已删除指令 ID 的 SEQ 仍可打开和原样保存，但相应行标红且 Run
预检会拒绝执行。完整声明格式、安全边界和自定义参数窗口接口见
[插件与模块开发](docs/PLUGIN_DEVELOPMENT.md)。

## 更换温度或磁场设备

不同温控器、磁体电源和 Monitor 使用各自的 Device Plugin，不需要为设备维护核心
分支。核心仓库只提供接口和 fail-closed 示例。部署实际插件时：

1. 把目标插件目录复制到 `device_plugins/`。
2. 把 `configs/default.toml` 复制为 `configs/site.local.toml`，只在副本中修改
   `plugin = "<plugin-id>"`、地址、安全上下限、速率和超时。
3. 用 `run.bat --config configs\site.local.toml` 启动并确认插件信任。发布包则使用
   `OpenLabControl.exe --config configs\site.local.toml`。

`*.local.toml` 默认不进入 Git；可提交的 `default.toml` 应继续保持仿真地址和脱敏内容。

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

- [开发者网站](https://yang-fu-physics.github.io/openlab-control/)
- [操作手册](docs/OPERATIONS.md)
- [SEQ 格式](docs/SEQUENCE_FORMAT.md)
- [DAT 与事件格式](docs/DAT_FORMAT.md)
- [配置参考](docs/CONFIGURATION.md)
- [系统架构](docs/ARCHITECTURE.md)
- [插件与模块开发](docs/PLUGIN_DEVELOPMENT.md)

## 真实仪表安全门槛

当前版本没有真实硬件验证，不应直接用于无人值守实验。接入真实仪表前必须完成只读
身份确认、有限通信超时、低风险写入、上下限三层校验、Hold 新鲜读回、写超时不重放、
失联恢复、Stop/Error、进程强杀和硬件互锁测试。软件进程隔离不能替代仪表自身的安全
状态、限流、限压、限温、磁体保护或人工急停。
