"""Measurement Module 作者唯一需要接触的后端 API。

模块无需继承框架类型。核心只要求 ``Module`` 对象实现 ``open``、``measure`` 和
``close``；本文件仅提供运行时传入的 ``ModuleAPI`` 以及可选的带编码异常。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import math
import time
from typing import Any


class ModuleError(RuntimeError):
    """带稳定编码的致命故障；普通异常同样会被核心视为 Error。"""

    def __init__(
        self,
        message: str,
        code: str = "MODULE_ERROR",
        key: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = key


class ModuleWarning(RuntimeError):
    """中止当前调用但允许 SEQ 继续的可恢复告警。"""

    def __init__(
        self,
        message: str,
        code: str = "MODULE_WARNING",
        key: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = key


class _ModuleOperationCancelled(RuntimeError):
    """Stop 在模块安全检查点触发的内部控制流。"""


@dataclass(slots=True)
class ModuleAPI:
    """一次模块调用可使用的四项能力。

    - :meth:`sleep`：Pause 不计时、Stop 可打断的等待。
    - :meth:`checkpoint`：在长循环或两次仪表 I/O 之间响应 Pause/Stop。
    - :meth:`instruments`：请求核心立即采样温度、磁场和 Monitor，再返回快照。
    - :meth:`resources`：读取扫描工具确认过的物理仪表地址表。
    - :meth:`warn`：报告或解除可恢复告警。
    - :meth:`status`：更新模块窗口中的只读状态。
    - :attr:`timeout`：本次调用的核心总时限，供模块预留安全清理时间。

    以下带下划线字段由 worker 注入，模块不得直接访问。
    """

    _initial_instruments: Mapping[str, Mapping[str, Any]]
    _emit: Callable[[str, dict[str, Any]], None]
    _sample_instruments: (
        Callable[[float], Mapping[str, Mapping[str, Any]]] | None
    ) = None
    _operation_state: Callable[[float], str] | None = None
    _operation_timeout_seconds: float = 120.0
    # 放在现有运行上下文字段之后。生产 worker 始终用关键字注入；保留这一顺序还可
    # 避免测试辅助类或第三方测试夹具把原来的 sample/state/timeout 位置参数错认成
    # 资源映射。模块作者不应直接访问带下划线字段，只调用 resources()。
    _instrument_resources: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )

    @property
    def timeout(self) -> float:
        """返回本次调用的总超时秒数。

        仪表模块应确保单次 I/O timeout 和必要的输出关闭时间小于该值；这项只读信息
        用于避免核心强制回收 worker 时来不及执行安全状态清理。
        """

        return self._positive_finite(
            self._operation_timeout_seconds,
            "Module operation timeout",
        )

    def sleep(
        self,
        seconds: float,
        *,
        poll_interval: float = 0.1,
    ) -> None:
        """等待指定秒数；Pause 冻结计时，Stop 抛出取消信号。

        ``sleep(0)`` 用于长循环或仪表 I/O 之间的轻量检查。第三方驱动自己的阻塞
        读写仍必须设置有限 I/O timeout，核心不能打断任意厂商 C 调用。
        """

        duration = self._nonnegative_finite(seconds, "Module sleep duration")
        interval = self._positive_finite(poll_interval, "Module sleep poll interval")
        request_timeout = min(
            1.0,
            self._positive_finite(
                self._operation_timeout_seconds,
                "Module operation timeout",
            ),
        )
        self._checkpoint(request_timeout)
        remaining = duration
        while remaining > 0:
            chunk = min(remaining, interval)
            started = time.monotonic()
            time.sleep(chunk)
            remaining -= max(0.0, time.monotonic() - started)
            self._checkpoint(request_timeout)

    def checkpoint(self) -> None:
        """立即执行一次 Pause/Stop 检查，不引入人为等待。

        它不能中断已经进入厂商驱动的阻塞调用，因此真实仪表仍须配置有限 I/O timeout。
        ``sleep(0)`` 保持相同效果；显式方法更适合表达长循环中的安全检查点。
        """

        request_timeout = min(
            1.0,
            self._positive_finite(
                self._operation_timeout_seconds,
                "Module operation timeout",
            ),
        )
        self._checkpoint(request_timeout)

    def instruments(
        self,
        timeout: float = 5.0,
    ) -> Mapping[str, Mapping[str, Any]]:
        """请求一次即时仪表采样并返回深拷贝。

        这条路径与约 1 秒一次的前面板刷新分离。真实测量模块在需要记录温度/场的
        时刻调用本方法，核心会立即轮询；同一时刻多个模块的请求会合并为一次轮询。
        纯单元测试没有核心回调时才回退到调用开始时的快照。
        """

        request_timeout = self._positive_finite(timeout, "Instrument snapshot timeout")
        self._checkpoint(min(request_timeout, 1.0))
        if self._sample_instruments is None:
            return deepcopy(dict(self._initial_instruments))
        latest = self._sample_instruments(request_timeout)
        if not isinstance(latest, Mapping):
            raise ModuleError(
                "The core returned an invalid instrument snapshot",
                "MODULE_SYSTEM_SNAPSHOT_INVALID",
            )
        normalized: dict[str, dict[str, Any]] = {}
        for instrument_id, values in latest.items():
            if not isinstance(values, Mapping):
                raise ModuleError(
                    "The core returned an invalid instrument snapshot",
                    "MODULE_SYSTEM_SNAPSHOT_INVALID",
                    str(instrument_id),
                )
            normalized[str(instrument_id)] = dict(values)
        self._initial_instruments = normalized
        return deepcopy(normalized)

    def resources(self) -> Mapping[str, Mapping[str, Any]]:
        """返回扫描并人工确认过的 Measurement 仪表资源。

        模块应把资源 ``id`` 保存到自己的设置中，在真正 ``open``/``configure`` 时再从
        本映射取得当前地址。返回值始终是深拷贝，模块不能修改核心配置，也不会获得
        System Instrument 地址；温度和磁场读数只能通过 :meth:`instruments`。
        """

        source = self._instrument_resources or {}
        if not isinstance(source, Mapping):
            raise ModuleError(
                "The core returned an invalid instrument resource table",
                "MODULE_INSTRUMENT_RESOURCE_INVALID",
            )
        result: dict[str, dict[str, Any]] = {}
        for resource_id, raw in source.items():
            if not isinstance(raw, Mapping):
                raise ModuleError(
                    "The core returned an invalid instrument resource",
                    "MODULE_INSTRUMENT_RESOURCE_INVALID",
                    str(resource_id),
                )
            values = dict(raw)
            if str(values.get("purpose", "")) != "measurement":
                raise ModuleError(
                    "The core exposed a non-measurement instrument resource",
                    "MODULE_INSTRUMENT_RESOURCE_INVALID",
                    str(resource_id),
                )
            result[str(resource_id)] = values
        return deepcopy(result)

    def resource(self, resource_id: str) -> Mapping[str, Any]:
        """按稳定 ID 返回一个 Measurement 仪表资源的深拷贝。"""

        selected = str(resource_id).strip()
        if not selected:
            raise ModuleError(
                "Select a measurement instrument resource",
                "MODULE_INSTRUMENT_RESOURCE_REQUIRED",
            )
        resource = self.resources().get(selected)
        if resource is None:
            raise ModuleError(
                f"Measurement instrument resource {selected!r} is unavailable",
                "MODULE_INSTRUMENT_RESOURCE_UNAVAILABLE",
                selected,
            )
        address = resource.get("address")
        if (
            not isinstance(address, str)
            or not address.strip()
            or any(not character.isprintable() for character in address)
        ):
            raise ModuleError(
                f"Measurement instrument resource {selected!r} has no valid address",
                "MODULE_INSTRUMENT_RESOURCE_INVALID",
                selected,
            )
        return deepcopy(dict(resource))

    def resource_address(self, resource_id: str) -> str:
        """解析稳定资源 ID；模块设置中不得保存原始 VISA 地址。"""

        return str(self.resource(resource_id)["address"]).strip()

    def warn(
        self,
        code: str,
        message: str | None,
        key: str = "",
    ) -> None:
        """报告告警；同一 ``code/key`` 传入 ``None`` 即解除告警。"""

        normalized_code = str(code).strip() or "MODULE_WARNING"
        normalized_key = str(key)
        if message is None:
            self._emit(
                "resolve",
                {"code": normalized_code, "context": normalized_key},
            )
            return
        self._emit(
            "warning",
            {
                "message": str(message),
                "code": normalized_code,
                "context": normalized_key,
            },
        )

    def status(self, values: Mapping[str, Any]) -> None:
        """发送小型、可 JSON 化的只读状态更新。"""

        self._emit("status", {"values": dict(values)})

    def _checkpoint(self, timeout: float) -> None:
        if self._operation_state is None:
            return
        while True:
            state = str(self._operation_state(timeout)).strip().casefold()
            if state in {"stopping", "cancelled"}:
                raise _ModuleOperationCancelled("Module operation was cancelled")
            if state != "paused":
                return
            time.sleep(0.05)

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
            raise ValueError(f"{label} must be a non-negative finite number")
        return result


__all__ = ["ModuleAPI", "ModuleError", "ModuleWarning"]
