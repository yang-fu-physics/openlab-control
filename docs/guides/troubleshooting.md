# 常见问题与排错

先保留当前运行目录和脱敏日志。不要为了“再试一次”连续重放可能改变输出的命令。

## 模块一直停留在 Initializing

检查顺序：

1. 查看 Modules 窗口底部的稳定错误码和 Context。
2. 查看模块是否在导入、构造或 `open()` 中执行了无界等待。
3. 确认所有 VISA/串口调用都有 timeout。
4. 确认 `open()` 不等待用户界面，也不等待 SEQ 事件。
5. 关闭应用后检查是否仍有模块 worker 进程。

首次 Enable 失败、重启后正常通常说明初始化时序、残留资源或 timeout 有问题，不应当把
“重启可恢复”视为修复。

## 找不到 VISA implementation

PyVISA 已由框架提供，但仍需要与 GPIB/USB 接口匹配的厂商 VISA Runtime。安装 NI-VISA、
Keysight VISA 或对应厂商 runtime 后重启程序。模块不应自行安装另一套 PyVISA。

## Enable 立即提示设置不合法

Enable 只应打开模块安全初态，不应把保存设置自动写入仪表。若模块必须验证界面设置，应在
用户点击 Apply Settings 时进行。对于旧保存值超出新量程的情况，Frontend 应显示并允许
用户修正，不应在构造阶段崩溃。

## Load SEQ 后模块参数没有作用

这是安全设计：`.seq` 的同名 `.modules.toml` 只导入界面值，不自动 Enable、连接或 Apply。
逐个打开 Enabled 模块的 Settings，检查后点击 Apply Settings。

## Measure 没有某个模块的数据

检查：

- 模块是否 Enabled；
- 是否在本次 Run 开始前已 Enabled；
- 模块声明的 `slots` 是否包含当前行；
- 模块是否返回了声明列；
- Warning 是否使本槽位测量值留空；
- DAT 是否使用旧列头打开了不兼容文件。

## 模块指令标红或 Run 前被拒绝

SEQ 可以保留缺失模块指令，但执行时必须满足：模块已安装、Enabled、仍声明相同稳定
`command_id`，并且参数通过当前声明校验。框架不会自动 Enable 或跳过。

## Live Trend 打开后设备超时

Live Trend 只应消费已有设备快照，不增加仪表轮询。若打开图表改变 timeout：

1. 检查 GUI 是否直接查询设备；
2. 检查重绘是否阻塞 Runtime 消息处理；
3. 限制绘制点数并合并刷新；
4. 对比打开前后的 `device_status.dat` 采样间隔和事件日志。

## Data Browser 不自动刷新

Data Browser 只追踪当前由用户打开的 DAT，并不与当前 Run 强制绑定。确认打开的是正在追加
的文件，文件仍存在，列头没有在写入过程中被外部程序改写。

## 停止后进程没有退出

`close`、worker shutdown 和进程回收都有界。如果厂商 C/COM 调用无界阻塞，核心最终只能
终止本机进程，无法保证仪表输出。修复驱动 timeout，并测试重复 close；不要只增加核心总
timeout。

## 4K 或弹窗出现横向滚动条

不要写死像素宽度。使用 Qt layout 和 sizeHint，让核心窗口尺寸工具计算“没有横向滚动条的
最小宽度”。长地址和路径应使用可收缩控件或省略显示，而不是撑大整个窗口。

## 报警 HTTP 失败

报警报告不是安全控制链。网络失败只形成去重的本地 Warning，不应阻塞 SEQ 或代替 Error
后的停机流程。检查 endpoint、X-Token 和测试员/管理员目标配置时不要把 token 写入日志。
