# OpenLab Control

OpenLab Control 是一个参考 Quantum Design MultiVu 操作方式、使用 Python/PySide6 编写的
实验控制框架。它不控制 PPMS。本项目把实验接入分成两类：

- **System Instrument**：主温控仪、主磁场控制器和长期只读监视仪表；
- **Measurement Module**：电阻、电压、电流、换向器等按实验需要加载的测量方案。

两者有不同目录、清单和生命周期。System Instrument 一般随实验系统固定；Measurement
Module 每次启动默认 Disabled，由用户按需 Enable。

当前稳定版本：`0.17.1`。默认配置全部使用内置仿真仪表，尚未完成真机验证。

开发者网站：<https://yang-fu-physics.github.io/openlab-control/>。网站包含面向初学者的中文
使用教程、Measurement Module 教程和完整 System Instrument 教程。

![主窗口](docs/main-window-preview.png)

## 主要能力

- MultiVu 风格的浮动 SEQ 编辑器，支持任意嵌套 Temperature、Field、Time 和模块扫描。
- Pause、Stop、Call Sequence、Set Datafile、Remark 和无参数 `Measure`。
- 配置中的温场上下限与最大速率同时约束手动控制、SEQ 参数窗口和运行时执行。
- 每个 System Instrument 实例和每个 Enabled Measurement Module 分别运行在独立子进程。
- 每种温度、磁场最多一个 `control_enabled = true` 的仪表；其他温磁仪表和 Monitor 只读。
- 主控仪表普通失联后按配置重连；默认每 2 秒尝试一次，60 秒后转为故障。
- 一个 `Measure` 按所有 Enabled 模块的通道槽位展开，每个通道一行；同一槽位的模块并行。
- 模块可返回多行结果、数字状态码和独立 `rawdata`；无结果或报错的值保持空单元格。
- Warning 继续运行并按 Source/Code/Context 去重；Error 中止 SEQ。
- Stop/Error 后温度和磁场保持当前状态，不自动回零；2nd Stage 只显示。
- 保存 SEQ 时同时保存模块设置伴随文件；Load SEQ 会导入设置，但不自动 Enable 或 Apply。
- Enabled 模块可在命令栏注册自己的 SEQ 指令；Disable 后立即移除。
- 模块监视卡显示在 `Sequence Status` 下，可查看最近值并恢复最小化的模块窗口。
- 独立 Data Browser 可打开任意 DAT 并自动追踪追加；绝对时间戳显示为实际日期时间，数值轴
  使用 `1/2/5 × 10ⁿ` 主刻度。
- Live Trend 使用已有快照缓存，不会为绘图增加仪表读取。
- 每次 Run 保存实验 DAT、事件、SEQ/配置快照和独立 `instrument_status.dat`。
- 可选异步 HTTP 报警报告：Warning 仅测试员，Error 同时通知管理员和测试员。
- **View → Appearance** 可分别调整整体界面和文字大小，并选择窗口尺寸策略。

## 读取顺序

System Instrument 的前面板完整读取和测量即时读取相互分开：

- `read_status()` 默认约每秒读取完整状态，用于前面板、安全检查、判稳和状态日志；
- `read_measurement()` 可只读取写测量行需要的即时主值；不实现时自动使用完整
  `read_status()`；
- 同一 System Instrument 不会并发访问。当前完整仪表指令先完成，等待中的控制/安全操作
  优先，测量即时读取再先于尚未开始的普通后台读取。

## 启动与测试

源码环境：

```text
setup.bat
run.bat
```

完整源码测试与无界面仿真：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe run.py --headless-demo --sequence examples\nested_scan.seq
```

Windows 文件夹发布包：

```text
build.bat
```

输出位于 `dist\OpenLabControl\`，其中包含同级的 `OpenLabControl.exe` 和
`InstrumentScanner.exe`。两个程序共享根目录下唯一的 `_internal`，发布包不建立
`tools/`；源码版扫描器继续使用主项目 `.venv` 中的锁定依赖。源码版和打包版都从程序旁的
`configs/`、`modules/`、
`system_instruments/`、`templates/` 和可写数据目录读取，不在 `_internal/` 放第二份副本。

正式稳定版由 GitHub Actions 自动构建：项目版本与 `v<版本>` 标签一致时，Windows Runner
会重新执行完整测试、严格文档构建、双 EXE 打包与 ZIP 检查，再直接创建 Release 并上传
ZIP 和 SHA-256。本地 `build.bat` 只用于发布前复核，不作为正式资产上传来源。

## Measurement Module

最小后台只需：

```python
class Module:
    def open(self, api): ...
    def measure(self, slot, api): ...
    def close(self, api): ...
```

会跨测量行保持输出的模块，应在 `on_event("run_end", ...)` 中按设置完成明确收尾。默认应在
本轮结束时关闭输出；确实需要连续偏置时，可以提供默认关闭的“保持输出”选项，并在保留前
回读确认。模块可按需增加 `configure`、`slots`、自定义界面和自己的 SEQ 指令。

第一次使用仿真模块：

1. 把 `templates/measurement-modules-repository/modules/simulated_transport` 完整复制到
   `modules/simulated_transport`。
2. 重启程序，打开 `Modules`。
3. 勾选 `Simulated Transport`，核对类型、ID、版本、路径和内容指纹后确认信任。
4. 运行含 `T Measure` 的 SEQ。

完整教程见[第一个 Measurement Module](docs/development/first-module.md)。

## System Instrument

核心只提供接口、内置仿真和 `templates/system-instruments-repository/` 中的 fail-closed
System Instrument 示例，不宣称一个示例可以控制任意真实仪表。正式实现应按具体型号、
固件、接线和现场安全范围编写。

安装时：

1. 把完整 System Instrument 文件夹复制到 `system_instruments/<instrument-id>/`。
2. 源码版运行 `.venv\Scripts\python.exe tools\instrument_scanner.py`；打包版双击根目录的
   `InstrumentScanner.exe`。程序打开后自动进行一次只读 VISA 扫描；TCP 仪表只输入地址和
   端口，不连接、不发送识别指令。随后由操作者确认地址、
   用途、System Instrument 和辅助读数；唯一识别的实现会自动选择，主读数由该实现固定，
   辅助读数使用复选框。结果写入 `configs/instruments.local.toml`。已有文件会先自动载入，
   保存前会逐项列出新增、替换、删除和保持不变的资源。
   System Instrument 名称和可选读数来自 `system_instruments/` 中的清单，扫描器不写死
   具体仪表，也不选择或绑定 Measurement Module。
3. 把 `configs/default.toml` 复制为 `configs/site.local.toml`。
4. 在现场副本的 `[[instruments]]` 中设置 `resource = "<资源 ID>"`、是否允许控制、安全
   上下限、最大速率和超时。实现、主读数、单位和显示精度由资源选择及仪表清单自动得到；
   `initial_value` 只属于内置仿真，不写入真实仪表条目。
5. 使用 `run.bat --config configs\site.local.toml` 启动；打包版使用
   `OpenLabControl.exe --config configs\site.local.toml`。
6. 核对首次信任窗口。发现目录不会连接仪表，只有现场配置引用的资源才会启动。

System Instrument 的后台以 `SystemInstrument` 为基类，提供同步的
`open/read_status/read_measurement/set_target/hold/execute_sequence_command/close`。通讯命令建议单独放在
`instrument.py`，让 `backend.py` 只处理生命周期、安全判断和读数组装。

一份 System Instrument 代码对应一种物理仪表型号/协议，不对应 TempA、TempB 这样的单个
通道。同一地址只建立一个会话；主读数写入 `value`，其余读数放入 `auxiliary` 字典，
System Instrument API 3 清单必须选择 `controller`、`readout`、`readout_grid` 或 `switch`
面板。`controller` 保持当前值、目标、速率和稳定状态样式；`readout` 只显示一个主读数；
`readout_grid` 以 2×2 最多显示四个读数，第五个开始放到右侧的下一个面板；`switch`
显示 0/1 状态，并把清单声明的无参数指令显示为按钮。同一指令
也直接出现在右侧 `System Commands` 中，不写 DAT。多个不同资源各有独立进程，可以同时
连接和并发轮询。

从[仪表扫描与地址配置](docs/guides/instrument-scanner.md)和
[System Instrument 教程](docs/development/system-instrument.md)开始学习。

## 依赖与完全离线安装

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 由框架统一提供并锁定版本。
所有 System Instrument 和 Measurement Module 使用同一份框架依赖，不重复安装。

只有确实需要框架外第三方包时，对应目录才携带：

- 精确 `==` 版本与 SHA-256 的 `requirements.lock`；
- 与目标 Windows/Python 匹配的本地 wheels。

程序只使用 `--no-index --require-hashes` 离线安装，不存在联网回退。额外依赖进入
`runtime_packages/<type>/<id>/<fingerprint>/site-packages/`，不会覆盖框架统一版本。

## 目录

```text
configs/                  主配置与现场配置
modules/                  手动安装的 Measurement Module（默认空）
system_instruments/       手动安装的 System Instrument（默认空）
module_data/<id>/         模块保存设置
runtime_packages/         可重建的额外依赖运行目录
trust_state/              本机内容信任记录
wheels/                   可选共享离线 wheels
templates/                Measurement Module 与 System Instrument 的独立示例
integrations/             NoneBot 报警接收器等外部集成参考
examples/                 SEQ、DAT、PLT 示例
runs/                     每次 Run 的数据、日志和快照
docs/                     操作、格式、架构和开发教程
src/labcontrol/           核心源码
tests/                    核心自动测试
tools/                    源码开发工具（发布包不复制该目录）
```

## 文档

- [开发者网站](https://yang-fu-physics.github.io/openlab-control/)
- [操作手册](docs/OPERATIONS.md)
- [Measurement Module 教程](docs/development/index.md)
- [System Instrument 教程](docs/development/system-instrument.md)
- [配置参考](docs/CONFIGURATION.md)
- [SEQ 格式](docs/SEQUENCE_FORMAT.md)
- [DAT 与事件格式](docs/DAT_FORMAT.md)
- [完整开发规范](docs/DEVELOPMENT_REFERENCE.md)

## 真实仪表安全门槛

当前版本没有真实硬件验证，不应直接用于无人值守实验。接入真实仪表前必须完成只读身份
确认、有限通讯超时、低风险写入、三层上下限、写后回读、Hold 新鲜读数、写超时不重放、
失联恢复、Stop/Error、进程强杀和硬件联锁测试。软件进程隔离不能替代限流、限压、限温、
磁体保护或人工急停。
