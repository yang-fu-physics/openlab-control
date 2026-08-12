# 公共 Python API

这一页只列出 System Instrument 和 Measurement Module 作者允许依赖的接口。
`measurement.worker`、`measurement.service`、
`sequence.engine`、UI 主窗口和 IPC 信封都是核心内部实现，不构成兼容承诺。

## Measurement Module 后端

模块不需要继承基类。下面的 API 对象由核心在每次调用时传入。

::: labcontrol.module_api.ModuleAPI
    options:
      members:
        - timeout
        - sleep
        - checkpoint
        - instruments
        - resources
        - warn
        - status

::: labcontrol.module_api.ModuleWarning

::: labcontrol.module_api.ModuleError

## Measurement Module 前端

Frontend 是普通 QWidget，只通过这个桥请求后端 Action 或状态刷新。

::: labcontrol.measurement.frontend_api.ModuleUIAPI
    options:
      members:
        - action
        - refresh
        - resources

## System Instrument

::: labcontrol.instruments.base.SystemInstrument
    options:
      members:
        - __init__
        - connect
        - disconnect
        - poll
        - poll_measurement
        - set_target
        - hold

::: labcontrol.instruments.base.InstrumentWarning

::: labcontrol.instruments.base.InstrumentError

::: labcontrol.instruments.base.SafetyViolation

## 仪表状态模型

::: labcontrol.models.InstrumentSnapshot

::: labcontrol.models.InstrumentMetric

::: labcontrol.models.InstrumentKind

::: labcontrol.models.InstrumentRole

::: labcontrol.models.InstrumentActivity

::: labcontrol.models.InstrumentConnectionState

::: labcontrol.models.StabilityState

## 公共接口使用原则

- Module 只导入 `labcontrol.module_api` 和可选 `labcontrol.measurement.frontend_api`。
- System Instrument 只依赖 `labcontrol.instruments.base`、配置对象和状态模型。
- 不保存 API 对象供另一个调用或线程使用。
- 不访问带下划线字段。
- 不通过内部 service/worker 绕过生命周期、IPC 或安全限制。
