"""System Instrument 自定义卡片与 Instrument Setup 弹窗的安全 UI 桥。

自定义前端运行在主 GUI 线程，绝不能直接持有串口、GPIB 或网络句柄。本接口把所有硬件查询与
写入异步提交到后台 ``RuntimeService``，并通过 Qt 信号把结果安全切回 GUI 主线程。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from copy import deepcopy
import json
from typing import Any

from PySide6.QtCore import QObject, Signal
from shiboken6 import isValid

from ..config import InstrumentConfig
from ..models import InstrumentSnapshot


ActionDispatcher = Callable[
    [str, str, dict[str, Any], bool],
    Future[dict[str, Any]],
]


class InstrumentUIAPI(QObject):
    """提供给 System Instrument 自定义卡片和设置界面的受限请求桥。"""

    snapshotChanged = Signal(object)
    editableChanged = Signal(bool)
    setupRequested = Signal(str)
    _actionFinished = Signal(int, bool, object)

    def __init__(
        self,
        config: InstrumentConfig,
        dispatcher: ActionDispatcher,
        parent: QObject | None = None,
        *,
        runtime_editable: bool = True,
        snapshot: InstrumentSnapshot | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._dispatcher = dispatcher
        self._runtime_editable = bool(runtime_editable)
        self._snapshot = snapshot
        self._next_token = 1
        self._callbacks: dict[
            int,
            tuple[
                Callable[[dict[str, Any]], None] | None,
                Callable[[str], None] | None,
            ],
        ] = {}
        self._actionFinished.connect(self._deliver_action_result)

    @property
    def instrument_id(self) -> str:
        return self._config.id

    @property
    def config(self) -> InstrumentConfig:
        """返回当前仪表配置的深拷贝，避免前端意外修改运行时共享字典。"""

        return deepcopy(self._config)

    @property
    def snapshot(self) -> InstrumentSnapshot | None:
        return deepcopy(self._snapshot) if self._snapshot is not None else None

    @property
    def connected(self) -> bool:
        return bool(self._snapshot is not None and self._snapshot.connected)

    @property
    def editable(self) -> bool:
        return self._runtime_editable and self.connected

    def update_snapshot(self, snapshot: InstrumentSnapshot) -> None:
        was_editable = self.editable
        self._snapshot = snapshot
        self.snapshotChanged.emit(snapshot)
        if self.editable != was_editable:
            self.editableChanged.emit(self.editable)

    def set_runtime_editable(self, editable: bool) -> None:
        was_editable = self.editable
        self._runtime_editable = bool(editable)
        if self.editable != was_editable:
            self.editableChanged.emit(self.editable)

    def open_setup(self) -> None:
        """请求主窗口打开 Instrument Setup 并定位到当前仪表。"""

        self.setupRequested.emit(self._config.id)

    def request(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> Future[dict[str, Any]]:
        """把 JSON 动作请求异步提交给当前物理仪表的串行后台队列。"""

        action_name = str(action).strip()
        if not action_name:
            raise ValueError("Instrument action name must not be empty")
        data = dict(payload or {})
        try:
            json.dumps(data, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Instrument action payload must be strict JSON: {exc}") from exc
        return self._dispatcher(self._config.id, action_name, data, bool(read_only))

    def call_action(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        read_only: bool = False,
        on_result: Callable[[dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> Future[dict[str, Any]]:
        """异步调用后台动作，并在 GUI 主线程中触发 ``on_result`` / ``on_error``。"""

        token = self._next_token
        self._next_token += 1
        self._callbacks[token] = (on_result, on_error)
        try:
            future = self.request(action, payload, read_only=read_only)
        except Exception as exc:
            self._callbacks.pop(token, None)
            if on_error is not None:
                on_error(str(exc))
            failed: Future[dict[str, Any]] = Future()
            failed.set_exception(exc)
            return failed

        def _done(done_future: Future[dict[str, Any]]) -> None:
            if not isValid(self):
                return
            try:
                value = done_future.result()
            except Exception as exc:
                try:
                    self._actionFinished.emit(token, False, str(exc))
                except RuntimeError:
                    return
            else:
                try:
                    self._actionFinished.emit(token, True, dict(value))
                except RuntimeError:
                    return

        future.add_done_callback(_done)
        return future

    def _deliver_action_result(
        self,
        token: int,
        ok: bool,
        payload: object,
    ) -> None:
        callbacks = self._callbacks.pop(token, None)
        if callbacks is None:
            return
        on_result, on_error = callbacks
        if ok:
            if isinstance(payload, dict):
                persisted = payload.get("persisted_config")
                if isinstance(persisted, Mapping):
                    self._config.extras.update(dict(persisted))
                if on_result is not None:
                    on_result(dict(payload))
            elif on_result is not None:
                on_result({})
        elif on_error is not None:
            on_error(str(payload))


__all__ = ["ActionDispatcher", "InstrumentUIAPI"]
