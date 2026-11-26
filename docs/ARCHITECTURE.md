# 系统架构

本文概览 OpenLab Control `0.20.9` 的进程结构、边界划分与核心运行机制，供系统维护者与核心贡献者参考。

## 1. 线程与进程拓扑

```text
GUI 主线程（PySide6）
├─ MainWindow / SEQ Editor / Data Browser / Live Trend
├─ Modules Manager 与各模块 Frontend（普通 QWidget）
└─ RuntimeService（线程安全异步桥）
            │
            ▼
后台 asyncio 运行时线程（唯一事件循环）
├─ InstrumentManager      系统仪表调度、限值校验、判稳、重连与事件响应
├─ SequenceEngine         SEQ 树解析执行、Pause / Stop / Fault 收尾
├─ MeasurementModuleService 模块生命周期、并行测量与结果校验
├─ DatRunLogger           所有数据与日志文件的唯一写入者
└─ EventManager / AlarmReporter
            │ 严格 JSON IPC（单帧 ≤ 1 MiB，禁止 pickle 与 NaN）
            ├─ 每个物理 System Instrument 实例 → 独立 spawn 子进程
            └─ 每个 Enabled Measurement Module  → 独立 spawn 子进程
```

- **GUI 不碰硬件**：界面通过消息队列接收只读快照，不直接持有通讯句柄；
- **进程隔离的作用边界**：隔离的是驱动阻塞与崩溃，不是恶意代码沙箱（模块 `frontend.py` 仍在主 GUI 进程中运行），因此只应安装来源可信的代码。

## 2. 两类扩展与共享依赖

- **System Instrument（`system_instruments/`）**：管理主温控、主磁场、开关和常驻监视表。后端继承同步的 `SystemInstrument`，返回普通字典，核心负责子进程、串行队列、限值校验、判稳与断线恢复；驱动还可按需通过 `frontend.py` 提供自定义底部卡片（`template = "custom"`）或按需加载的 **Instrument Setup** 弹窗界面（`setup_widget`），所有硬件读写仍经 `InstrumentUIAPI` 走子进程串行队列。
- **Measurement Module（`modules/`）**：管理按需加载的测量方案。最小目录只需 `module.toml` + `backend.py`（无需继承基类），扫描发现时不导入源码，Enable 后在子进程实例化。
- **框架共享依赖**：`PySide6`、`QtAwesome`、`packaging`、`PyVISA` 和 `typing_extensions` 由框架统一锁定和打包，扩展不重复声明或维护第二套 Python 环境。

## 3. 配置与地址边界

```text
configs/general.default.toml         随发布包提供的通用配置默认模板（只作首次初始化与升级参考）
configs/general.toml                 本机通用设置（首次启动从默认模板自动复制，UI、日志、报警、超时、重连）
system_instruments/<id>/instrument.toml  作者模板（字段、控制端点、固定面板、读数、指令）
        ▼ Instrument Scanner
configs/instruments/<id>.toml        现场实例（[[instances]]、面板顺序、角色与限值）
configs/visa.resources.toml          未分配给系统仪表的 Measurement VISA（[[resources]]）
configs/module_instances.toml        可选的测量模块同源码多实例（[[instances]]）
configs/pid/<instance-id>.toml       首次复制、后续保留的独立现场 PID 文件
```

- 同一物理仪表的多个面板与控制回路共用同一个子进程和连接；
- `sample_temp` 和 `field` 角色全局各最多 1 个，其余面板为 `none`；没有任何系统面板也能启动；
- 扫描器保存时按预览全量原子重写生成文件并清理标为 `DELETE` 的文件，但不覆盖或删除现有 `configs/pid/`。

## 4. 调度、优先级与数据流

### 系统仪表优先级串行队列

每台物理系统仪表所有操作经过同一个优先串行入口，绝不并发访问同一端口，也不会中途强行打断已经发出的指令：

1. **安全联动响应（`EventResponseSpec`）** 优先级最高；
2. **手动/SEQ 控制写命令（`set_target` / `hold` / 系统指令）** 次之；
3. **测量专用即时读取（`read_measurement()`）** 优先于尚未开始的常规轮询；
4. **后台常规状态轮询（`read_status()`）** 最后。

空闲时按 `poll_interval_seconds` 轮询；SEQ 控制期间按 `control_poll_interval_seconds` 采样判稳，而发给前端和 Live Trend 的快照仍按常规周期节流。

### 测量模块执行与收尾

```text
Enable  → 启动子进程 → 验证列/指令元数据 → open(api) → 注册自定义指令
Apply   → 校验跨模块 VISA 地址占用 → configure(settings, api)
Run     → 冻结已启用模块与 slots → on_event("run_start")
Measure → 按逻辑槽位并集并行调用各参与模块 measure(slot, api) → 合并写行
Finish  → completed / stopped / error 统一调用 on_event("run_end", reason)
Disable → close(api) → 有界回收子进程
```

- SEQ 完成、Stop、Error 或取消都不会主动给系统仪表发 `Set` 或 `Hold`；只有明确注册的事件响应才触发跨仪表联动；
- 系统仪表读链路普通失联后进入自动重连窗（默认 60 秒）并在恢复后核对状态；写命令超时属于结果不确定，直接进入 Faulted，绝不重发；
- 同一 `Source + Code + Context` 活动事件自动去重，Warning 继续、Error 中止 SEQ。

## 5. 硬件安全边界

2026-11-05 已现场确认完成 Lake Shore 340 温控、ZLAN 压缩机控制和 Keithley 6221 + 2182A Delta 测量的实机测试（仅覆盖已测接线与配置）。软件隔离和限值不能替代仪表本机保护、硬件联锁和人工急停。
