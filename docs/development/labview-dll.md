# 优先复用 LabVIEW DLL

如果目标仪表已经有经过实际使用的 LabVIEW 驱动或测量 VI，优先把这部分编译为 DLL 接入
OpenLab Control。不要仅仅为了改用 Python，就重新翻译整套仪表指令、状态字和命令顺序。

这样做主要减少的是重新实现仪表协议带来的风险，例如写错量程、漏读状态、改变原有触发
顺序或误判异常响应。它不表示任意 LabVIEW 程序天然安全：既有的上下限、有限超时、
写后回读、按仪表约定的输出收尾和真机低风险测试仍然必须保留。

只有在没有可复用的 LabVIEW 实现，或原实现不能满足这些安全要求时，才按
[一台仪表一个文件](instrument-drivers.md)重新编写 Python/VISA 底层。

## 先判断放在哪里

同一个 DLL 接口可以用于两类扩展，但生命周期不同：

| DLL 完成的工作 | 放置位置 |
| --- | --- |
| 主温度、主磁场、持续监控或简单系统开关 | System Instrument |
| 在 `Measure` 时完成电阻、电压、电流、切换通道等测量 | Measurement Module |

不要因为使用 DLL 就改变原来的职责边界。多台仪表共同完成一次测量时，它们仍应属于同一个
Measurement Module；一台温控仪的多个读数仍属于同一个 System Instrument 实例。

## 从模板开始

两类仓库都提供 `examples/labview_dll/`。复制与目标类型对应的整个目录：

```text
System Instrument：system_instruments/<新 ID>/
Measurement Module：modules/<新 ID>/
```

本页的模板与自动调试窗口要求 OpenLab Control `0.20.1` 或更新版本。

复制后至少修改清单、后台类、DLL 文件名和真实安全范围，并把 LabVIEW 构建生成的 DLL 及其
依赖文件放在同一目录。示例中的 discovery pattern、列名、读数和参数都只是占位内容，不能
直接连接真实仪表。

模板保留一层很薄的 Python 后台。它不是重复实现仪表协议，而是把 OpenLab 生命周期转换为
四个固定 DLL 调用，并负责把 DLL 的 Warning/Error 转回框架。仪表命令、状态解析和原有测量
流程继续留在 LabVIEW 中。

## DLL 只导出四个函数

```text
OLC_Describe  返回静态能力，不连接仪表
OLC_Open      建立连接并确认仪表
OLC_Invoke    执行一次完整的高层操作
OLC_Close     按仪表约定收尾并释放资源
```

四个函数都使用模板 `openlab_labview_abi.h` 中的 C (`cdecl`) 签名。输入和输出是 UTF-8
JSON 的 U8 数组；不要改成 LabVIEW String Handle。一次完整查询或测量应尽量只跨一次 DLL
边界，不要把每条底层 VISA 指令分别导出。

`OLC_Describe` 只能返回常量说明。它在 worker 初始化时执行；如果这里连接仪表、等待触发或
启动循环，界面会一直停在 Initializing。

## 生命周期怎样对应

System Instrument 的对应关系：

```text
open                  -> OLC_Open
read_status           -> OLC_Invoke("read_status")
read_measurement      -> OLC_Invoke("read_measurement")
set_target / hold     -> OLC_Invoke(...)
简单 SEQ 指令          -> OLC_Invoke("sequence_command")
close                 -> OLC_Close
```

System Instrument 在 SEQ 完成或停止时保持当前控制状态；应用退出时 `close` 只负责约定的
连接收尾，不应因为复用 DLL 而新增归零、停止 ramp 或关闭温控输出。

Measurement Module 的对应关系：

```text
Enable                -> OLC_Open
Apply Settings        -> OLC_Invoke("configure")
Measure               -> OLC_Invoke("measure")
run_start / run_end   -> OLC_Invoke("event")
普通指令 / Scan 每点   -> OLC_Invoke("sequence_command")
Disable / 应用退出     -> OLC_Close
```

Measurement 模板同时给出一个普通指令和一个 Scan 指令。修改真实模块时只保留确实需要的
指令，并在 DLL 内再次检查参数；SEQ 参数窗口不是安全边界。普通指令和 Scan 点本身不生成
DAT 行，只有显式的 `Measure` 才写测量数据。

## 额外函数自动生成窗口

远程排查时偶尔需要调用不属于标准生命周期的 LabVIEW 功能。不要为这些功能另写 Python
底层或手工制作 Qt 窗口；DLL 可在 `OLC_Describe` 的 `manual_functions` 中直接说明输入和输出，
OpenLab 会自动生成窗口。例如：

```json
"manual_functions": [
  {
    "id": "diagnostic_read",
    "label": "Diagnostic Read",
    "description": "Run one existing LabVIEW diagnostic VI.",
    "inputs": [
      {"name": "mode", "label": "Mode", "type": "choice", "default": "Current", "choices": ["Current", "Voltage"]},
      {"name": "range", "label": "Range", "type": "choice", "default": "Auto", "choices": ["Auto", "Low", "High"]},
      {"name": "level", "label": "Level", "type": "float", "default": 0.001, "minimum": 0.0, "maximum": 1.0, "unit": "A", "decimals": 9},
      {"name": "samples", "label": "Samples", "type": "int", "default": 10, "minimum": 1, "maximum": 1000}
    ],
    "outputs": [
      {"name": "value", "label": "Value", "type": "float", "unit": "V", "decimals": 9},
      {"name": "status", "label": "Status", "type": "int"}
    ]
  }
]
```

输入支持 `text`、`int`、`float`、`choice`、`bool` 和 `list`；枚举使用 `choice` 并给出
`choices`。输出支持 `text`、`int`、`float` 和 `bool`。这份说明在 DLL 内，不需要 TOML 或
框架源码再登记一次。

System Instrument 加载后，函数出现在主菜单 `Instrument` 下；Measurement Module 只有在
Enable 成功后才出现在 `Modules` 下。每次选择都会打开一个独立窗口，因此可以同时保留多个
窗口。Run 会进入该仪表或模块已经存在的 worker 串行执行：不会再次加载 DLL、不会建立第二条
仪表连接，也不会与同一后端的其他调用并发。

两个枚举会显示为两个下拉框，整数和小数使用数字文本框；小数字段可直接输入 `1e-12`，
避免界面默认精度把小数改成零。若输入不符合 DLL 给出的类型或范围，Run 会直接指出问题。
手动函数尚未结束时不能开始 SEQ。关闭调试窗口只关闭显示，不会中断已经发给仪表的操作。

调用使用：

```json
{
  "operation": "manual_function",
  "parameters": {
    "function_id": "diagnostic_read",
    "parameters": {"mode": "Current", "range": "Auto", "level": 0.001, "samples": 10},
    "operation_timeout_seconds": 10.0
  }
}
```

DLL 的 `result` 必须恰好包含声明过的输出字段。函数只能在 SEQ 空闲时运行；结果显示在窗口并
进入应用事件日志，不生成 DAT 测量行。尚未创建运行目录时，事件与现有日志一样先保存在内存，
下一次创建运行目录时写入事件文件。框架不知道任意调试函数的物理含义，因此只应暴露已经在
LabVIEW 内保留参数检查、仪表限制和安全联锁的高层 VI，不能借此提供绕过保护的任意指令入口。

## 错误必须完整返回

每次调用都返回固定信封：

```json
{
  "severity": "ok",
  "code": "",
  "message": "",
  "context": "",
  "result": {}
}
```

把 LabVIEW error cluster 转成稳定的 `code`、可读的 `message` 和具体的 `context`：

- `warning`：结束本次模块调用，但允许 SEQ 继续；
- `error`：中止 SEQ；
- 非零 C 返回值：只表示坏指针、缓冲区不足等连 JSON 都无法返回的 ABI 故障。

测量值异常时不要把错误文字写入数字列。按模块自己的定义写数值状态码，并省略无效测量值。
通讯、系统状态或安全状态无法确认时必须返回 Error，不能用空值掩盖。

## Runtime 和位数

自动窗口是 OpenLab 生成的 PySide6 界面；保留在 DLL 里的测量 VI 仍由 LabVIEW Runtime 执行。
这里优先复用的是经过验证的 LabVIEW 仪表实现。

LabVIEW 编译的 DLL 仍然需要 LabVIEW Run-Time Engine，不需要完整的 LabVIEW 开发环境。
目标电脑必须满足：

- OpenLab Control、DLL、DLL 依赖和 Runtime 位数一致；发布版 OpenLab 使用 64 位；
- Runtime 版本能够加载生成 DLL 的 LabVIEW 版本；
- DLL 构建产物中的依赖文件完整放置；
- 发布或复制前在没有 LabVIEW 开发环境的电脑上做一次加载测试。

Runtime 不由 OpenLab 的 Python 依赖自动提供。应把所需版本和位数写进该仪表或模块的安装
说明，并使用 [NI 官方 Runtime 下载页](https://www.ni.com/en/support/downloads/software-products/download.labview-runtime.html)。

## Pause、Stop 和超时

框架只能在进入 DLL 前和 DLL 返回后处理 Pause/Stop，无法中断已经进入 DLL 的阻塞 VI。
因此：

- 每次 VISA、串口或厂商库调用都必须有有限超时；
- 很长的等待尽量返回 Python，由 `api.sleep()` 完成；
- 写命令超时后不得盲目重发，应重新连接并回读真实状态；
- 核心强制结束 worker 时，不能保证 DLL 还有机会执行 `OLC_Close`；
- 输出保护不能只依赖软件，仍需使用仪表本机限制和硬件联锁。

## 提交前检查

- DLL 在目标电脑上只安装 Runtime 也能加载；
- `OLC_Describe` 不进行 I/O，并能快速返回；
- Open 失败后 Close 仍能释放部分初始化资源；
- 正常完成、Stop、Error 和 Disable 都经过预期的安全收尾；
- Warning/Error 的 code、message、context 没有丢失；
- 所有控制值在 DLL 内复核，危险写入后读取仪表真实状态；
- 真机从最低风险设置开始，逐项核对命令顺序、状态位和约定的输出保持/关闭策略。

模板测试只验证 Python 包装层和 ABI 约定，不能代替实际 LabVIEW DLL、Runtime 与真实仪表
联合测试。
