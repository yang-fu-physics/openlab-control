# 运行第一条 SEQ

SEQ 是缩进树，而不是逐行拼接的任意脚本。Scan 可以任意嵌套，`Measure` 明确决定何时
写入测量行。

## 最小示例

```text
T Remark "First simulated run"
T Set Datafile open|create experiment.dat
T Set Temperature 300 K at 10 K/min in Settle mode
T Scan Field 0 Oe to 200 Oe in 3 steps at 5000 Oe/min, Settle
T     Measure
T End Scan
T End Sequence
```

在图形界面中可以双击右侧指令打开参数窗口并插入，不需要手写文本。保存时，缩进和
`End Scan` 由编辑器维护。

## 执行过程

1. Run 前解析整棵树、展开 Call Sequence，并检查递归与缺失模块指令。
2. Set Temperature/Field 同时受到参数窗口、配置和运行时三处边界检查。
3. Scan 每移动到一点并满足模式要求后，递归执行它的子树。
4. `Measure` 读取一次温场快照，并按逻辑 slot 写一行或多行。
5. 完成、Stop 或 Error 都通知模块 `run_end`；Stop/Error 后温场保持当前值。

## Pause 与 Stop

- Pause 冻结框架等待和 `ModuleAPI.sleep()` 计时，不强行关闭模块输出。
- Stop 在安全检查点取消正在进行的模块调用，并要求可控温场设备 Hold Current。
- 厂商驱动内已经阻塞的 I/O 不能被 Python 安全打断，因此每次 I/O 必须有有限 timeout。

## 运行目录

每次 Run 都保存自己的快照：

```text
runs/<timestamp>_<sequence>/
├─ sequence.seq
├─ configuration.toml
├─ module_settings/
├─ rawdata/
├─ experiment.dat
├─ device_status.dat
└─ events.dat
```

下一步阅读 [第一个测量模块](../development/first-module.md)，让 `Measure` 写入自定义列。
