"""独立于仪表状态字的温度/磁场稳定性判定。

判定同时要求误差进入容差、滑动窗口斜率足够小，并连续保持 ``dwell_seconds``。任何一个
条件失效都会重新开始 dwell；读数超时和长期不更新分别产生 TIMED_OUT 与 STALE。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .config import StabilityConfig
from .models import StabilityState


@dataclass(frozen=True, slots=True)
class StabilityResult:
    """一次判定结果及用于诊断的误差、斜率和计时信息。"""

    state: StabilityState
    error: float
    slope_per_minute: float | None
    qualified_seconds: float
    elapsed_seconds: float


class StabilityEvaluator:
    """以实际采样计算为主，并允许仪表稳定标志作为额外必要条件。"""

    def __init__(self, config: StabilityConfig) -> None:
        self.config = config
        self._target: float | None = None
        self._target_started_at: float | None = None
        self._qualified_at: float | None = None
        self._last_sample_at: float | None = None
        self._history: deque[tuple[float, float]] = deque()
        self._last_result = StabilityResult(
            StabilityState.MOVING, float("inf"), None, 0.0, 0.0
        )

    def reset(self, target: float, now: float) -> None:
        """目标改变时清空历史和已累计的稳定驻留时间。"""

        self._target = target
        self._target_started_at = now
        self._qualified_at = None
        self._last_sample_at = None
        self._history.clear()

    def update(
        self,
        current: float,
        target: float,
        now: float,
        *,
        instrument_stable: bool | None = None,
    ) -> StabilityResult:
        """加入新样本并计算 MOVING、SETTLING、STABLE 或 TIMED_OUT。

        ``instrument_stable`` 为 ``False`` 时禁止开始或继续 dwell；``True`` 也不能
        单独判稳，仍必须通过核心的误差、斜率和驻留时间检查。``None`` 保留没有该
        状态位的仪表原有行为。
        """

        if self._target is None or target != self._target:
            self.reset(target, now)
        self._last_sample_at = now
        self._history.append((now, current))
        cutoff = now - self.config.window_seconds
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        error = abs(current - target)
        slope = self._slope_per_minute()
        span = self._history[-1][0] - self._history[0][0] if len(self._history) > 1 else 0.0
        enough_history = len(self._history) >= 3 and span >= min(0.5, self.config.window_seconds / 2.0)
        within = error <= self.config.tolerance
        slope_ok = slope is not None and abs(slope) <= self.config.max_slope_per_minute
        elapsed = now - self._target_started_at if self._target_started_at is not None else 0.0

        # 仪表状态只作附加门槛，不能绕过软件独立计算的误差、历史和斜率条件。
        qualified_by_instrument = instrument_stable is not False
        if within and enough_history and slope_ok and qualified_by_instrument:
            if self._qualified_at is None:
                self._qualified_at = now
            qualified = now - self._qualified_at
            state = (
                StabilityState.STABLE
                if qualified >= self.config.dwell_seconds
                else StabilityState.SETTLING
            )
        else:
            self._qualified_at = None
            qualified = 0.0
            state = StabilityState.SETTLING if within else StabilityState.MOVING

        if elapsed >= self.config.timeout_seconds and state is not StabilityState.STABLE:
            state = StabilityState.TIMED_OUT

        self._last_result = StabilityResult(state, error, slope, qualified, elapsed)
        return self._last_result

    def check_stale(self, now: float) -> StabilityResult:
        """在没有新样本时检查最后一次读数是否已经过期。"""

        if self._last_sample_at is not None and now - self._last_sample_at > self.config.stale_after_seconds:
            self._last_result = StabilityResult(
                StabilityState.STALE,
                self._last_result.error,
                self._last_result.slope_per_minute,
                0.0,
                self._last_result.elapsed_seconds,
            )
        return self._last_result

    def _slope_per_minute(self) -> float | None:
        """用滑动窗口内的最小二乘直线计算每分钟变化率。"""

        if len(self._history) < 2:
            return None
        base = self._history[0][0]
        xs = [time_value - base for time_value, _ in self._history]
        ys = [value for _, value in self._history]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denominator = sum((value - x_mean) ** 2 for value in xs)
        if denominator <= 1e-15:
            return 0.0
        slope_per_second = sum(
            (x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)
        ) / denominator
        return slope_per_second * 60.0
