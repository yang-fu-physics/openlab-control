"""把核心事件异步发送到外部报警接收端。

这里的 HTTP 报警只负责“尽力通知”，不是仪表互锁的一部分。网络不可用、接收端离线或
程序退出超时都不能阻塞 SEQ 的 Error 处理、已注册事件响应或仪表进程回收。因此实现采用
有界队列和独立后台线程，并把最终发送失败重新报告为本地 Warning。

收件人不由发射端指定。发射端只发送 Warning/Error 级别及事件内容，管理员和测试员的
路由完全由接收端配置，避免持有 Token 的客户端任意选择 QQ 目标。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from .config import AlarmReportingConfig
from .models import EventNotice, Severity


DeliveryStateCallback = Callable[[str | None], None]
UrlOpen = Callable[..., Any]
_STOP = object()
_MAX_MESSAGE_CHARACTERS = 3500
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class AlarmReport:
    """一次报警投递的不可变快照。

    ``event_id`` 在全部重试中保持不变，接收端可以据此实现幂等去重；``level`` 只允许
    由上游 Event 的严重级别生成，不接受 UI 或远端输入直接覆盖。
    """

    event_id: str
    level: str
    message: str

    def payload(self) -> bytes:
        """生成紧凑 UTF-8 JSON；禁止 NaN，避免产生非标准 JSON。"""

        return json.dumps(
            {
                "event_id": self.event_id,
                "level": self.level,
                "message": self.message,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


class AlarmReporter:
    """非阻塞、可重试且有容量上限的 Warning/Error HTTP 发射端。

    主运行线程只调用 :meth:`handle_notice` 做一次非阻塞入队。DNS、TCP、TLS 和 HTTP
    等可能长时间等待的工作全部在 ``OpenLabAlarmReporter`` 线程完成。队列满时宁可
    丢弃新通知并在本地留下 Warning，也不能反向拖住仪表安全动作。
    """

    def __init__(
        self,
        config: AlarmReportingConfig,
        project_root: Path,
        delivery_state_callback: DeliveryStateCallback | None = None,
        *,
        opener: UrlOpen = urlopen,
    ) -> None:
        self.config = config
        self.project_root = project_root.resolve()
        self._delivery_state_callback = delivery_state_callback
        self._opener = opener
        # 队列必须有上限。报警接收端长期离线时，无界队列会持续占用内存，并可能把一个
        # 次要的通知故障放大成主程序崩溃。
        self._queue: queue.Queue[AlarmReport | object] = queue.Queue(
            maxsize=config.queue_size
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._callback_lock = threading.Lock()
        self._token = ""

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def start(self) -> None:
        """校验凭据后启动发送线程；配置或 Token 无效时保持 fail-closed。"""

        if not self.enabled or self._thread is not None:
            return
        try:
            self._token = self._load_token()
        except (OSError, ValueError) as exc:
            self._notify_failure(str(exc))
            return
        self._thread = threading.Thread(
            target=self._run,
            name="OpenLabAlarmReporter",
            daemon=True,
        )
        self._thread.start()

    def handle_notice(self, notice: EventNotice) -> None:
        """接收 EventManager 通知并尝试非阻塞入队。

        RESOLVED、Info 和报警发射端自身产生的事件都不向外发送。最后一条限制用于阻断
        “报警发送失败 -> 新报警 -> 再次发送失败”的反馈回路。
        """

        if (
            not self.enabled
            or self._thread is None
            or notice.is_resolution
            or notice.event.severity
            not in {Severity.WARNING, Severity.ERROR}
            or notice.event.source == "alarm_reporter"
        ):
            return
        event = notice.event
        identity = (
            f"{event.key}|{event.timestamp.isoformat()}|"
            f"{event.severity.value}"
        )
        # key 标识 Source/Code/Context，时间戳区分同一故障恢复后的再次发生，severity
        # 区分 Warning 升级为 Error。重试复用同一 event_id，便于接收端幂等处理。
        event_id = hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        report = AlarmReport(
            event_id=event_id,
            level=event.severity.value,
            message=self._format_message(notice),
        )
        try:
            self._queue.put_nowait(report)
        except queue.Full:
            self._notify_failure(
                "Alarm reporting queue is full; a new alarm "
                "could not be queued"
            )

    def close(self, timeout_seconds: float | None = None) -> None:
        """在给定总时限内停止后台线程，不无限等待远端网络。

        优先放入哨兵，让已排队任务按正常路径收尾；队列已满或线程没有按时退出时再设置
        stop Event。这里不会为了“保证消息送达”延长应用关闭，因为仪表和模块资源释放
        的优先级更高。
        """

        thread = self._thread
        if thread is None:
            self._disable_callback()
            return
        timeout = (
            self.config.shutdown_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        try:
            self._queue.put(
                _STOP,
                timeout=min(timeout, 0.25),
            )
        except queue.Full:
            self._stop.set()
        thread.join(timeout)
        if thread.is_alive():
            self._stop.set()
            thread.join(min(0.25, timeout))
        self._thread = None
        self._disable_callback()

    def _run(self) -> None:
        """后台线程主循环；每个 ``get`` 都与一次 ``task_done`` 成对。"""

        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item is _STOP:
                    return
                assert isinstance(item, AlarmReport)
                self._deliver_with_retries(item)
            finally:
                self._queue.task_done()

    def _deliver_with_retries(self, report: AlarmReport) -> None:
        """用同一个 event_id 重试，且等待可被关闭请求立即打断。"""

        last_error: BaseException | None = None
        for attempt in range(1, self.config.retry_attempts + 1):
            if self._stop.is_set():
                return
            try:
                self._deliver(report)
            except Exception as exc:
                last_error = exc
                if attempt < self.config.retry_attempts:
                    self._stop.wait(
                        self.config.retry_delay_seconds
                    )
                continue
            self._notify_success()
            return
        assert last_error is not None
        self._notify_failure(
            f"Alarm delivery failed after "
            f"{self.config.retry_attempts} attempt(s): "
            f"{type(last_error).__name__}: {last_error}"
        )

    def _deliver(self, report: AlarmReport) -> None:
        """执行一次有超时的 HTTP POST，并限制接收端响应体大小。"""

        request = Request(
            self.config.endpoint,
            data=report.payload(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Token": self._token,
                "User-Agent": "OpenLab-Control-AlarmReporter",
            },
        )
        with self._opener(
            request,
            timeout=self.config.timeout_seconds,
        ) as response:
            status_value = getattr(
                response,
                "status",
                None,
            )
            if status_value is None:
                status_value = response.getcode()
            status = int(status_value)
            body = response.read(
                _MAX_RESPONSE_BYTES + 1
            )
        # urllib 通常会把非 2xx 变成 HTTPError，但测试 opener 或其他实现未必如此，
        # 所以仍显式检查状态码，不把“收到响应”误判为“报警已接收”。
        if not 200 <= status < 300:
            raise OSError(
                f"Alarm receiver returned HTTP {status}"
            )
        if len(body) > _MAX_RESPONSE_BYTES:
            raise OSError(
                "Alarm receiver response exceeded 64 KiB"
            )

    def _load_token(self) -> str:
        """从环境变量或独立文件读取 Token，绝不从普通配置正文回显秘密。

        环境变量优先，文件只作离线部署的替代方案。长度和字符集限制既能发现误配，也
        避免把整份配置文件或带换行内容意外放进 HTTP Header。
        """

        token = ""
        if self.config.token_env:
            token = os.environ.get(
                self.config.token_env,
                "",
            ).strip()
        if not token and self.config.token_file:
            path = Path(self.config.token_file)
            if not path.is_absolute():
                path = self.project_root / path
            if path.stat().st_size > 4096:
                raise ValueError(
                    "Alarm reporting token file exceeds 4 KiB"
                )
            token = path.read_text(
                encoding="utf-8-sig"
            ).strip()
        if not token:
            raise ValueError(
                "Alarm reporting is enabled but no token was "
                "found in token_env or token_file"
            )
        if (
            len(token) > 1024
            or any(
                character.isspace()
                or ord(character) < 33
                or ord(character) == 127
                for character in token
            )
        ):
            raise ValueError(
                "Alarm reporting token must be at most 1024 "
                "non-whitespace printable characters"
            )
        return token

    @staticmethod
    def _format_message(notice: EventNotice) -> str:
        """生成适合即时消息阅读的文本，并限制远端单条消息长度。"""

        event = notice.event
        lines = [
            f"【OpenLab Control {event.severity.value.upper()}】",
            f"Time: {event.timestamp.astimezone().isoformat()}",
            f"Host: {socket.gethostname()}",
            f"Source: {event.source}",
            f"Code: {event.code}",
        ]
        if event.context:
            lines.append(f"Context: {event.context}")
        lines.append(f"Message: {event.message}")
        message = "\n".join(lines)
        if len(message) <= _MAX_MESSAGE_CHARACTERS:
            return message
        suffix = "\n[message truncated]"
        return (
            message[
                : _MAX_MESSAGE_CHARACTERS - len(suffix)
            ]
            + suffix
        )

    def _notify_success(self) -> None:
        """通知核心清除“报警发送失败”Warning；回调异常不得杀死发送线程。"""

        callback = self._callback()
        if callback is not None:
            try:
                callback(None)
            except Exception:
                pass

    def _notify_failure(self, message: str) -> None:
        """把最终投递失败转为本地状态，不递归投递该 Warning。"""

        callback = self._callback()
        if callback is not None:
            try:
                callback(message)
            except Exception:
                pass

    def _callback(self) -> DeliveryStateCallback | None:
        with self._callback_lock:
            return self._delivery_state_callback

    def _disable_callback(self) -> None:
        with self._callback_lock:
            self._delivery_state_callback = None


__all__ = ["AlarmReport", "AlarmReporter"]
