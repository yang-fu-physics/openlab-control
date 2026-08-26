"""随框架提供的开发教程四通道电阻模块。

这个示例不连接硬件。它的目的不是模拟某一种具体仪表，而是把 Measurement Module 的
公开后端接口集中展示在一个可运行文件中。真实模块应把 VISA/串口命令移到独立驱动文件，
并保留相同的生命周期、安全检查和返回值边界。
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import re
from statistics import fmean, pstdev
from typing import Any

from labcontrol.module_api import ModuleAPI, ModuleError


STATUS_NORMAL = 0
STATUS_OVER_RANGE = 1

_CURRENT_PATTERN = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\s*([pnum]?)a?$",
    re.IGNORECASE,
)
_CURRENT_PREFIXES = {
    "": 1.0,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
}


class Module:
    """无需继承基类；核心只查找本类公开的约定名称。"""

    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "ExcitationCurrent": "A",
        "ResistanceStdDev": "Ohm",
        "SampleCount": "",
        "StatusCode": "",
    }
    display_columns = ("R1", "R2", "R3", "R4")
    slots = 4

    # 声明只包含受限 JSON 元数据。模块成功 Enable 后，核心才会把这些指令加入右侧栏。
    sequence_commands = [
        {
            "id": "set_current",
            "label": "Set Current",
            "description": "Set the simulated excitation current without writing DAT.",
            "kind": "command",
            "fields": [
                {
                    "name": "current",
                    "label": "Current",
                    "type": "text",
                    "default": "1 mA",
                },
                {
                    "name": "settle_seconds",
                    "label": "Settle time",
                    "type": "float",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 60.0,
                    "unit": "s",
                    "decimals": 3,
                },
            ],
        },
        {
            "id": "scan_current",
            "label": "Scan Current",
            "description": "Scan current points and run nested commands after each point.",
            "kind": "scan",
            "points_field": "points",
            "point_parameter": "current",
            "fields": [
                {
                    "name": "points",
                    "label": "Current points",
                    "type": "list",
                    "default": ["100 uA", "200 uA", "500 uA"],
                },
                {
                    "name": "settle_seconds",
                    "label": "Settle time",
                    "type": "float",
                    "default": 0.0,
                    "minimum": 0.0,
                    "maximum": 60.0,
                    "unit": "s",
                    "decimals": 3,
                },
            ],
        },
    ]

    def __init__(self) -> None:
        self.opened = False
        self.applied = False
        self.run_active = False
        self.output_enabled = False
        self.current_a = 1e-3
        self.settings = self._defaults()

    @staticmethod
    def _defaults() -> dict[str, float]:
        return {
            "base_resistance_ohm": 100.0,
            "channel_step_ohm": 10.0,
            "delay_seconds": 0.02,
            "noise_ohm": 0.001,
            "over_range_ohm": 1e6,
        }

    def open(self, api: ModuleAPI) -> Mapping[str, Any]:
        """Enable 阶段只进入安全初态，不自动应用保存设置。"""

        self.opened = True
        self.applied = False
        self.run_active = False
        self.output_enabled = False
        status = self._status()
        api.status(status)
        return status

    def configure(
        self,
        settings: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """仅在用户点击 Apply Settings 后接收并验证界面值。"""

        if not self.opened:
            raise ModuleError("Module is not open", "TUTORIAL_NOT_OPEN")
        merged: dict[str, Any] = {**self._defaults(), **dict(settings)}
        normalized = {
            key: self._finite_number(merged[key], key)
            for key in self._defaults()
        }
        if normalized["base_resistance_ohm"] <= 0:
            self._invalid_setting("base_resistance_ohm", "must be positive")
        if normalized["channel_step_ohm"] < 0:
            self._invalid_setting("channel_step_ohm", "must not be negative")
        if not 0 <= normalized["delay_seconds"] <= 60:
            self._invalid_setting("delay_seconds", "must be between 0 and 60 s")
        if normalized["noise_ohm"] < 0:
            self._invalid_setting("noise_ohm", "must not be negative")
        if normalized["over_range_ohm"] <= 0:
            self._invalid_setting("over_range_ohm", "must be positive")

        self.settings = normalized
        self.applied = True
        status = self._status()
        api.status(status)
        return status

    def on_event(
        self,
        event: str,
        data: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """把一次 SEQ 的输出状态与一次测量调用分开管理。"""

        if event == "run_start":
            self._require_ready()
            self.run_active = True
            self.output_enabled = True
        elif event == "run_end":
            # completed、stopped 和 error 都执行同一安全收尾；close 不会在每个 Run 后调用。
            self.run_active = False
            self.output_enabled = False
        elif event != "status":
            return {}
        status = self._status()
        status["Last Run Result"] = str(data.get("reason", "—"))
        api.status(status)
        return status

    def measure(
        self,
        slot: int,
        api: ModuleAPI,
    ) -> tuple[dict[str, float | int], list[float]]:
        """测量一个逻辑通道并最多返回一行；绝不在模块内循环四个通道。"""

        self._require_ready()
        if slot not in {1, 2, 3, 4}:
            raise ModuleError(
                "Invalid logical slot",
                "TUTORIAL_INVALID_SLOT",
                str(slot),
            )

        api.checkpoint()
        api.sleep(self.settings["delay_seconds"])
        instruments = api.instruments()
        temperature = float(
            instruments.get("temperature", {}).get("current") or 300.0
        )
        field_oe = float(instruments.get("field", {}).get("current") or 0.0)
        center = (
            self.settings["base_resistance_ohm"]
            + self.settings["channel_step_ohm"] * (slot - 1)
            + 0.01 * (temperature - 300.0)
            + 1e-8 * field_oe**2
        )
        noise = self.settings["noise_ohm"]
        raw_values = [center - noise, center, center + noise]
        resistance = fmean(raw_values)
        stddev = pstdev(raw_values)

        channel = f"R{slot}"
        over_range = abs(resistance) > self.settings["over_range_ohm"]
        row: dict[str, float | int] = {
            "ExcitationCurrent": self.current_a,
            "ResistanceStdDev": stddev,
            "SampleCount": len(raw_values),
            "StatusCode": STATUS_OVER_RANGE if over_range else STATUS_NORMAL,
        }
        if over_range:
            # 数据异常保留本行和数值状态，但无效电阻列必须为空。
            api.warn(
                "OVER_RANGE",
                f"{channel} exceeds the configured simulated range",
                channel,
            )
        else:
            row[channel] = resistance
            api.warn("OVER_RANGE", None, channel)

        api.status(
            {
                "Last Channel": channel,
                "Last Resistance (Ohm)": "—" if over_range else resistance,
            }
        )
        return row, raw_values

    def execute_sequence_command(
        self,
        command_id: str,
        parameters: Mapping[str, Any],
        api: ModuleAPI,
    ) -> Mapping[str, Any]:
        """普通指令和 Scan 的每个点都通过同一个受控入口执行。"""

        self._require_ready()
        if command_id not in {"set_current", "scan_current"}:
            raise ModuleError(
                f"Unknown command: {command_id}",
                "TUTORIAL_UNKNOWN_COMMAND",
                command_id,
            )
        current = self._parse_current(parameters.get("current"))
        settle = self._finite_number(
            parameters.get("settle_seconds", 0.0),
            "settle_seconds",
        )
        if not 0 <= settle <= 60:
            self._invalid_setting("settle_seconds", "must be between 0 and 60 s")

        # 真实驱动应在这里再次检查硬件量程，再发送一次有界写命令并完成读回确认。
        self.current_a = current
        api.sleep(settle)
        status = {"Excitation Current (A)": current}
        api.status(status)
        return status

    def close(self, api: ModuleAPI) -> Mapping[str, Any]:
        """允许部分初始化或重复调用，先关闭输出，再释放本地状态。"""

        self.output_enabled = False
        self.run_active = False
        self.applied = False
        self.opened = False
        status = self._status()
        api.status(status)
        return status

    def _status(self) -> dict[str, Any]:
        return {
            "Connection": "Open (simulation)" if self.opened else "Closed",
            "Applied Settings": "Yes" if self.applied else "No",
            "Sequence": "Running" if self.run_active else "Idle",
            "Output": "On" if self.output_enabled else "Off",
            "Excitation Current (A)": self.current_a,
        }

    def _require_ready(self) -> None:
        if not self.opened:
            raise ModuleError("Module is not open", "TUTORIAL_NOT_OPEN")
        if not self.applied:
            raise ModuleError(
                "Review the settings and click Apply Settings first",
                "TUTORIAL_SETTINGS_NOT_APPLIED",
                "settings",
            )

    @staticmethod
    def _finite_number(value: object, field: str) -> float:
        if isinstance(value, bool):
            Module._invalid_setting(field, "must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ModuleError(
                f"{field} must be numeric",
                "TUTORIAL_INVALID_SETTINGS",
                field,
            ) from exc
        if not math.isfinite(number):
            Module._invalid_setting(field, "must be finite")
        return number

    @staticmethod
    def _parse_current(value: object) -> float:
        if isinstance(value, bool):
            Module._invalid_setting("current", "must be numeric")
        if isinstance(value, (int, float)):
            current = float(value)
        else:
            text = str(value).strip().casefold().replace("μ", "u").replace("µ", "u")
            match = _CURRENT_PATTERN.fullmatch(text)
            if match is None:
                Module._invalid_setting(
                    "current",
                    "use A, mA, uA, nA, pA or the short forms m/u/n/p",
                )
            assert match is not None
            current = float(match.group(1)) * _CURRENT_PREFIXES[match.group(2)]
        if not math.isfinite(current) or not 0 < abs(current) <= 10e-3:
            Module._invalid_setting("current", "must be non-zero and within ±10 mA")
        return current

    @staticmethod
    def _invalid_setting(field: str, detail: str) -> None:
        raise ModuleError(
            f"{field} {detail}",
            "TUTORIAL_INVALID_SETTINGS",
            field,
        )
