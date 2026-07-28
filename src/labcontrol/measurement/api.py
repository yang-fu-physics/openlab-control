"""Measurement Module 后端可使用的稳定 API 契约。

模块代码运行在独立子进程中，本文件中的对象会由框架在 worker 内构造。模块只能通过
``ModuleOperationContext`` 读取核心提供的系统快照、报告状态/数据或协作检查
Pause/Stop；它不能直接持有主进程的 DeviceManager、Qt 对象或 DAT writer。
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
import time
from typing import Any


class ModuleError(RuntimeError):
    """不可恢复的模块或仪表故障。

    活动 SEQ 收到此异常后会进入 Error 路径并尝试 Hold 温度/磁场。它不等同于进程崩溃；
    模块仍应在 ``end_sequence("error")`` 或随后 ``abort`` 中尽力释放仪表资源。
    """

    def __init__(self, message: str, code: str = "MODULE_ERROR", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class ModuleWarning(RuntimeError):
    """本次操作可恢复的告警；框架允许 SEQ 继续。

    只有确实能继续并保持数据语义可信时才应使用 Warning。通信中断、设置读回不一致、
    无法确认输出安全等情况应使用 :class:`ModuleError`。
    """

    def __init__(self, message: str, code: str = "MODULE_WARNING", context: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class ModuleOperationCancelled(RuntimeError):
    """SEQ Stop 时在 worker 内部触发的协作取消信号。

    这是框架控制流，不应被模块转换为 Warning 或 Error。模块可用 ``finally`` 做分流、
    关闭输出等清理，然后让异常继续向外传播。
    """


@dataclass(slots=True)
class ModuleOperationContext:
    """模块一次生命周期调用可见的只读系统上下文和输出通道。

    ``system`` 是请求开始时的温度、磁场和 Monitor 快照。长时间测量需要更新值时必须
    调用 :meth:`sample_system`；直接反复读取 ``system`` 只会得到上一次快照。

    所有发送内容最终都要经过 JSON 和 Schema 校验。这里提供接口不表示模块可以绕过
    主进程验证，也不表示成功发送一行就已经写入 DAT。
    """

    system: Mapping[str, Mapping[str, Any]]
    _emit: Callable[[str, dict[str, Any]], None]
    _sample_system: (
        Callable[[float], Mapping[str, Mapping[str, Any]]] | None
    ) = None
    _operation_state: Callable[[float], str] | None = None
    operation_timeout_seconds: float = 120.0

    def emit_row(self, values: Mapping[str, Any]) -> None:
        """流式发送一行测量结果；列名必须已在 ``module.toml`` 中声明。"""

        self._emit("row", {"values": dict(values)})

    def update_status(self, values: Mapping[str, Any]) -> None:
        """更新模块窗口状态；不得把大对象或不可 JSON 化的驱动对象放入状态。"""

        self._emit("status", {"values": dict(values)})

    def warning(self, message: str, code: str = "MODULE_WARNING", context: str = "") -> None:
        """报告可恢复 Warning，不终止当前生命周期方法。"""

        self._emit("warning", {"message": message, "code": code, "context": context})

    def resolve_warning(self, code: str = "MODULE_WARNING", context: str = "") -> None:
        """解除与 ``code/context`` 精确匹配的活动 Warning。"""

        self._emit("resolve", {"code": code, "context": context})

    def error(self, message: str, code: str = "MODULE_ERROR", context: str = "") -> None:
        """立即抛出致命模块故障，由核心进入 SEQ Error 路径。"""

        raise ModuleError(message, code, context)

    def sample_system(
        self,
        timeout_seconds: float = 5.0,
    ) -> Mapping[str, Mapping[str, Any]]:
        """获取核心当前拥有的最新温度、磁场和 Monitor 快照。

        返回值是深拷贝，模块修改它不会影响核心状态。调用前先经过一次 Stop/Pause
        checkpoint，因此 Stop 不会被新的快照请求长期推迟。没有实时 sampler 的纯后端
        单元测试会得到初始 ``system`` 的拷贝。
        """

        timeout = self._positive_finite(
            timeout_seconds,
            "System snapshot timeout",
        )
        self.checkpoint(timeout)
        if self._sample_system is None:
            return deepcopy(dict(self.system))
        latest = self._sample_system(timeout)
        if not isinstance(latest, Mapping):
            raise ModuleError(
                "The core returned an invalid system snapshot",
                "MODULE_SYSTEM_SNAPSHOT_INVALID",
            )
        normalized: dict[str, dict[str, Any]] = {}
        for device_id, values in latest.items():
            if not isinstance(values, Mapping):
                raise ModuleError(
                    "The core returned an invalid device snapshot",
                    "MODULE_SYSTEM_SNAPSHOT_INVALID",
                    str(device_id),
                )
            normalized[str(device_id)] = dict(values)
        self.system = normalized
        return deepcopy(normalized)

    def checkpoint(self, timeout_seconds: float = 1.0) -> None:
        """在安全检查点协作响应 Pause/Stop。

        Paused 时此方法留在 worker 中等待，不占用主 GUI 线程；Stopping/Cancelled 时
        抛出 :class:`ModuleOperationCancelled`。仪表驱动自己的阻塞读写仍必须设置有限
        超时，因为框架无法在任意第三方 C/驱动调用中插入 checkpoint。
        """

        timeout = self._positive_finite(
            timeout_seconds,
            "Module checkpoint timeout",
        )
        if self._operation_state is None:
            return
        while True:
            state = str(self._operation_state(timeout)).strip().casefold()
            if state in {"stopping", "cancelled"}:
                raise ModuleOperationCancelled("Module measurement was cancelled")
            if state != "paused":
                return
            # 这里只阻塞模块 worker。较短睡眠可避免忙等，同时让 Stop 能迅速被观察到。
            time.sleep(0.05)

    def interruptible_sleep(
        self,
        seconds: float,
        *,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        """可被 Stop 打断、且不把 Pause 时间计入剩余时长的等待。

        模块的 pause/dwell/settle 等实验计时应使用此方法，不应直接 ``time.sleep`` 一
        个很长的周期。轮询间隔决定 Stop 响应上限，通常保持默认值即可。
        """

        duration = self._nonnegative_finite(seconds, "Module sleep duration")
        poll_interval = self._positive_finite(
            poll_interval_seconds,
            "Module sleep poll interval",
        )
        # poll_interval 只决定多久检查一次 Stop/Pause，不应同时成为一次跨进程
        # context RPC 的超时。首次 spawn 或系统短暂繁忙时，Windows 调度很容易超过
        # 100 ms；若把默认轮询间隔直接传给 Pipe 等待，几十毫秒的普通初始化也会
        # 随机失败。RPC 最多使用一秒，但仍受本次模块操作的总 deadline 限制。
        checkpoint_timeout = min(
            1.0,
            self._positive_finite(
                self.operation_timeout_seconds,
                "Module operation timeout",
            ),
        )
        remaining = duration
        while remaining > 0:
            self.checkpoint(checkpoint_timeout)
            interval = min(remaining, poll_interval)
            started = time.monotonic()
            time.sleep(interval)
            remaining -= max(0.0, time.monotonic() - started)
            self.checkpoint(checkpoint_timeout)

    @staticmethod
    def _positive_finite(value: float, label: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive finite number") from exc
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{label} must be a positive finite number")
        return result

    @staticmethod
    def _nonnegative_finite(value: float, label: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{label} must be a non-negative finite number"
            ) from exc
        if not math.isfinite(result) or result < 0:
            raise ValueError(
                f"{label} must be a non-negative finite number"
            )
        return result


class ModuleBackend(ABC):
    """Measurement Module 在独立 worker 进程中的生命周期契约。

    方法有意设计成同步接口，便于直接调用 VISA/串口等传统驱动；框架也兼容实现返回
    awaitable。无论哪种方式，模块都必须给每次仪表通信配置有限超时。框架的进程超时能
    回收本机 worker，却不能证明外部仪表已经停止输出。

    固定顺序为：

    ``initialize``（Enable）→ ``apply_settings``（用户确认后，可多次）→
    ``begin_sequence`` → ``measure``（可多次）→ ``end_sequence``。

    Disable 或应用退出使用 ``abort``。Stop 只结束本次 SEQ 并调用
    ``end_sequence("stopped")``，不会自动 Disable 模块。
    """

    api_version = "1.0"

    def initialize(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any] | None:
        """Enable 时初始化模块。

        可以发现通信资源并加载设置，但不应仅因 Enable 就连接并自动 Apply 已保存的
        仪表参数，更不能无确认地打开激励输出。
        """

        return None

    def apply_settings(
        self, settings: Mapping[str, Any], context: ModuleOperationContext
    ) -> Mapping[str, Any] | None:
        """用户在 Settings 页确认后发送设置，并应读取仪表回读进行核对。"""

        return None

    def begin_sequence(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        """在第一条 SEQ 指令前调用，用于建立本次运行的临时状态。"""

        return None

    def measure(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        """执行一次 Measure。

        多行结果应逐行调用 ``context.emit_row``；返回 Mapping 可额外产生一行。一个
        Measure 会在不同 Enabled 模块之间并行，但同一模块的请求始终串行。
        """

        return None

    def end_sequence(
        self, reason: str, context: ModuleOperationContext
    ) -> Mapping[str, Any] | None:
        """结束本次 SEQ，``reason`` 为 completed/stopped/error 之一。

        此处应关闭只属于本次运行的输出或临时状态，但模块仍保持 Enabled。实现必须允许
        在 Measure 部分完成或异常后调用。
        """

        return None

    def abort(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        """Disable 或应用退出时尽力进入模块定义的安全状态并释放通信资源。

        应设计为可重复调用，并在内部使用有限通信超时。返回成功才表示模块确认了自己的
        清理步骤；worker 被框架强杀只代表本机进程已回收，不代表仪表安全。
        """

        return None

    def read_status(self, context: ModuleOperationContext) -> Mapping[str, Any] | None:
        """只读刷新实际仪表状态，不应借此隐式 Apply 或改变输出。"""

        return None

    def manual_action(
        self,
        action: str,
        payload: Mapping[str, Any],
        context: ModuleOperationContext,
    ) -> Mapping[str, Any] | None:
        """处理 SEQ Idle 时的模块自定义手动动作。"""

        raise ModuleWarning(f"Unsupported manual action: {action}", "UNSUPPORTED_ACTION", action)
