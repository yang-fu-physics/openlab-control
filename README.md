# OpenLab Control

OpenLab Control 是一个参考 Quantum Design MultiVu 操作方式、使用 Python/PySide6 编写的
实验控制框架。它不控制 PPMS。本项目把实验接入分成两类：

- **System Instrument**：主温控仪、主磁场控制器和长期只读监视仪表；
- **Measurement Module**：电阻、电压、电流、换向器等按实验需要加载的测量方案。

两者有不同目录、清单和生命周期。System Instrument 一般随实验系统固定；Measurement
Module 每次启动默认 Disabled，由用户按需 Enable。

当前稳定版本：`0.20.0`。全新安装不启用任何 System Instrument 面板；三个内置仿真也都
默认关闭，需要时在 Instrument Scanner 中明确选择。当前版本尚未完成真机验证。

开发者网站：<https://yang-fu-physics.github.io/openlab-control/>。网站包含面向初学者的中文
使用教程、Measurement Module 教程和完整 System Instrument 教程。

![主窗口](docs/main-window-preview.png)

## 主要能力

- MultiVu 风格的浮动 SEQ 编辑器，支持任意嵌套 Temperature、Field、Time 和模块扫描。
- Pause、Stop、Call Sequence、Set Datafile、Remark 和无参数 `Measure`。
- 配置中的温场上下限与最大速率同时约束手动控制、SEQ 参数窗口和运行时执行。
- 每个 System Instrument 实例和每个 Enabled Measurement Module 分别运行在独立子进程。
- `sample_temp` 和 `field` 角色各自全局最多一个；其他面板使用 `none`，只显示、记录或提供
  自己的系统指令。
- 主控仪表普通失联后按配置重连；默认每 2 秒尝试一次，60 秒后转为故障。
- 一个 `Measure` 按所有 Enabled 模块的通道槽位展开，每个通道一行；同一槽位的模块并行。
- 模块可返回多行结果、数字状态码和独立 `rawdata`；无结果或报错的值保持空单元格。
- Warning 继续运行并按 Source/Code/Context 去重；Error 中止 SEQ。
- SEQ 完成、Stop 或 Error 都不控制 System Instrument；只有仪表明确注册的事件响应才会执行联动动作。2nd Stage 只显示。
- 保存 SEQ 时同时保存模块设置伴随文件；Load SEQ 会导入设置，但不自动 Enable 或 Apply。
- Enabled 模块可在命令栏注册自己的 SEQ 指令；Disable 后立即移除。
- 模块监视卡显示在 `Sequence Status` 下，可查看最近值并恢复最小化的模块窗口。
- 每次打开 Data Browser 都会创建独立窗口，可同时比较多个 DAT；数据区的 View 会直接载入
  当前 Run 文件，文件追加后自动刷新。
- 默认实验 DAT 只保存时间戳、主控温度和模块声明列；多通道模块每行只填写当前通道结果。
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

完整源码测试：

```text
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

无界面运行包含 Temperature/Field 的示例前，先在 Instrument Scanner 中明确启用所需仿真。

Windows 文件夹发布包：

```text
build.bat
```

输出位于 `dist\OpenLabControl\`，其中包含同级的 `OpenLabControl.exe` 和
`InstrumentScanner.exe`。两个程序共享根目录下唯一的 `_internal`，发布包不建立
`tools/`；源码版扫描器继续使用主项目 `.venv` 中的锁定依赖。源码版和打包版都从程序旁的
`configs/`、`modules/`、`system_instruments/` 和可写数据目录读取，不在 `_internal/`
放第二份副本。

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

第一次使用仿真模块：打开 `Modules`，Enable 已随框架提供的 `Simulated Transport`，再运行
含 `T Measure` 的 SEQ。模块仍然每次启动都保持 Disabled。

完整教程见[第一个 Measurement Module](docs/development/first-module.md)。

## System Instrument

核心只提供接口、三个可选仿真和 `system_instruments/` 中的简单示例，不宣称示例可以控制
任意真实仪表。正式实现应按具体型号、固件、接线和现场安全范围编写。

安装与配置：

1. 把完整目录复制到 `system_instruments/<instrument-id>/`。
2. 源码版运行 `.venv\Scripts\python.exe tools\instrument_scanner.py`；打包版双击根目录的
   `InstrumentScanner.exe`。
3. 第一页确认 VISA 地址。留给 Measurement Module 的未分配地址写入
   `configs/visa.resources.toml`，每条只有 `id/address/identity`。某地址分配给 System
   Instrument 后会从这份清单移除。
4. 在对应 System Instrument 页面添加一个或多个物理实例，确认型号专用字段、固定面板、
   角色、读数、控制限制和稳定参数。专用网络仪表由自己的 API v4 清单声明 `host`、`port`
   等字段，在该型号页面填写。
5. 最后一页选择是否启用三个仿真、调整全部面板的全局顺序并检查完整预览。保存会完整覆盖
   预览中的 `configs/instruments/<instrument-id>.toml`，并删除标为 DELETE 的其他生成文件。
6. 重启 OpenLab Control，检查连接状态、前面板和 Run Log。没有 System 面板也是有效状态。

扫描时没有发现、但已经保存的 Measurement VISA 会灰显并默认保留；取消保留才会删除。
System Instrument 的 PID 字段第一次保存时把作者示例或操作者选择的文件复制到
`configs/pid/<instance-id>.toml`。以后扫描器不会覆盖或删除现有 PID 文件；若示例含空
`zones`，必须先填写现场验证值，否则相应后台会阻止启动。

System Instrument 的后台以 `SystemInstrument` 为基类，提供同步的
`open/read_status/read_measurement/set_target/hold/execute_sequence_command/event_responses/close`。通讯命令建议单独放在
`instrument.py`，让 `backend.py` 只处理生命周期、安全判断和读数组装。

一份 System Instrument 代码对应一种物理型号或协议，不对应 TempA、TempB 这样的单个通道。
同一物理实例只建立一个会话；主读数写入 `value`，其余读数放入 `auxiliary`。API v4
`instrument.toml` 用 `config_fields`、`controls`、`panels`、`readings` 和
`sequence_commands` 描述表单、控制端点、固定面板、读数和无参数指令。实例只引用固定
panel ID，并保存 `enabled/order/role/reading` 与 controller 限制。一个文件可包含多个
`[[instances]]`，不同物理实例各有独立进程，可以并发轮询。

同一物理实例可以声明多个独立 Controller 回路，它们仍共用一个 worker 和通讯会话。
只有一个控制端点时，`read_status()` 可在顶层返回 `target/rate/moving/ready`；启用了多个
不同控制端点时，必须按作者清单中的 control ID 返回 `controls` 字典，不能让多个面板
共享一份目标或稳定状态。

从[仪表扫描与地址配置](docs/guides/instrument-scanner.md)和
[System Instrument 教程](docs/development/system-instrument.md)开始学习。

## 依赖与完全离线安装

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 由框架统一提供并锁定版本。
所有 System Instrument 和 Measurement Module 直接使用同一份框架依赖，不建立独立
runtime，也不在界面中安装依赖。新仪表确实需要第三方包时，应把精确版本加入核心
`pyproject.toml` 与 `requirements-lock.txt`，完整测试并重新构建离线发布包。
源码运行时，`setup.bat` 会用 `requirements-lock.txt` 建立 `.venv`；发布 EXE 则已包含同一
组依赖，不要求现场电脑另装 Python 包。

## 目录

```text
configs/general.toml      唯一通用配置
configs/visa.resources.toml  未分配的 Measurement VISA（扫描器生成）
configs/instruments/      System Instrument 实例（扫描器生成）
configs/pid/              每个物理实例的 PID 文件
modules/                  Measurement Module 与随框架提供的示例
system_instruments/       System Instrument 与随框架提供的示例
module_data/<id>/         模块保存设置
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
确认、有限通讯超时、低风险写入、三层上下限、写后回读、手动 Hold 新鲜读数、写超时不重放、
失联恢复、SEQ 退出不控制仪表、注册事件响应、进程强杀和硬件联锁测试。软件进程隔离不能替代限流、限压、限温、
磁体保护或人工急停。
