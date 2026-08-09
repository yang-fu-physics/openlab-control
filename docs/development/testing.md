# 测试扩展

测试分成三层：纯函数与协议、模块/插件生命周期、真实仪表低风险验证。前两层通过不代表
第三层可以省略。

## 教程模块测试

配套测试不启动完整 GUI，直接创建 Module 和 ModuleAPI：

```python
api = ModuleAPI(
    {
        "temperature": {"current": 300.0},
        "field": {"current": 0.0},
    },
    lambda kind, payload: events.append((kind, payload)),
)

module = Module()
module.open(api)
module.configure({}, api)
row, rawdata = module.measure(1, api)

assert row["StatusCode"] == 0
assert "R1" in row
assert len(rawdata) == 3
module.close(api)
```

运行模板测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover `
  -s plugin_templates\measurement-modules-repository\tests -v
```

## Backend 必测行为

- `open` 成功、部分失败和身份不符；
- 未 Apply 时拒绝测量；
- 每个 slot 只返回一行，其他通道列为空；
- 正常、超量程、compliance 和作者状态码；
- rawdata 有限、长度有界并与正式行对应；
- Pause/Stop 检查点；
- `run_end` 的 completed/stopped/error；
- 重复 `close` 和异常清理；
- 自定义普通/扫描指令的非法参数和未知 ID。

## 协议层必测行为

使用 Fake VISA/Serial session 断言：

- 身份查询发生在任何写命令之前；
- 设置、读回和触发命令的完整顺序；
- 每种量程和状态字解析；
- 坏响应、空响应和非有限数；
- 写 timeout 后不盲目重放；
- 关闭输出失败时仍继续释放其他资源；
- 每个 I/O 的 timeout 小于总操作上限。

## Frontend 测试

在 Qt offscreen 环境验证：

- `load` 后 `dump` 保持数据；
- 加载设置不触发 action 或后端连接；
- `show_status` 只更新已有控件；
- Settings 窗口在最小内容宽度下无横向滚动条；
- 滚轮不会在未展开控件上误改值。

## 核心回归测试

如果扩展需要修改核心公共接口，应先运行受影响测试，再运行完整测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

核心改动还必须覆盖 SEQ 解析/格式化往返、任意嵌套、缺失模块预检、IPC timeout、进程退出、
动态 DAT 列和窗口重建。

## 真实仪表验证顺序

1. 不连接样品，只做身份和只读状态。
2. 使用仪表本机最低风险上下限。
3. 单次写入后立即读回，不开始长 SEQ。
4. 人工触发 Stop、断线和 timeout。
5. 验证仪表面板、硬件联锁和急停。
6. 保存脱敏命令日志和测试结果。
7. 完成后才能移除 Beta 标记或考虑无人值守。
