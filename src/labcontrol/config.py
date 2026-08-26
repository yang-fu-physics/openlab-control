"""严格读取并验证主配置文件。

配置对象在启动时一次性构造为不可变 dataclass，之后由 UI、运行时、System Instrument 和
Measurement Module 共同读取。
所有影响真实仪表安全的值（上下限、速率、超时、主仪表角色）都在进入运行时前验证；不能
依赖某个对话框临时校验，因为无界面模式和第三方调用同样会使用这些配置。
"""

from __future__ import annotations

import math
import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .instrument_resources import (
    InstrumentResource,
    InstrumentResourceError,
    load_instrument_resources,
)
from .models import InstrumentKind, Severity


class ConfigurationError(ValueError):
    """配置缺字段、类型错误或违反框架安全约束。"""


_WINDOWS_RESERVED_FILE_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


@dataclass(frozen=True, slots=True)
class StabilityConfig:
    """框架独立稳定性判定参数，单位沿用对应仪表的显示单位。"""

    tolerance: float
    max_slope_per_minute: float
    dwell_seconds: float
    timeout_seconds: float
    window_seconds: float
    stale_after_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class InstrumentReadingConfig:
    """一个读数的稳定键和显示元数据。"""

    key: str
    display_name: str
    unit: str = ""
    decimals: int | None = None


@dataclass(frozen=True, slots=True)
class InstrumentCommandConfig:
    """One generated stable command exposed by a physical instance."""

    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class InstrumentPanelConfig:
    """One fixed panel view bound to a physical instrument instance."""

    id: str
    instrument_id: str
    display_name: str
    template: str
    enabled: bool
    order: int | None = None
    role: str = "none"
    control_id: str = ""
    reading: str = ""
    readings: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    min_value: float = float("-inf")
    max_value: float = float("inf")
    default_rate_per_minute: float = 1.0
    max_rate_per_minute: float = float("inf")
    stability: StabilityConfig | None = None

    @property
    def key(self) -> str:
        return f"{self.instrument_id}.{self.id}"


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    """One physical instance and its single backend/session configuration."""

    id: str
    display_name: str
    kind: InstrumentKind
    backend: str
    panels: tuple[InstrumentPanelConfig, ...] = ()
    address: str = ""
    control_enabled: bool = False
    unit: str = ""
    main_reading: str = "value"
    auxiliary_readings: tuple[str, ...] = ()
    readings: tuple[InstrumentReadingConfig, ...] = ()
    sequence_commands: tuple[InstrumentCommandConfig, ...] = ()
    initial_value: float = 0.0
    default_rate_per_minute: float = 1.0
    min_value: float = float("-inf")
    max_value: float = float("inf")
    max_rate_per_minute: float = float("inf")
    stale_after_seconds: float = 3.0
    operation_timeout_seconds: float = 10.0
    shutdown_timeout_seconds: float = 3.0
    stability: StabilityConfig | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def reading(self, key: str) -> InstrumentReadingConfig:
        for reading in self.readings:
            if reading.key == key:
                return reading
        raise KeyError(key)

    def panel(self, panel_id: str) -> InstrumentPanelConfig:
        for panel in self.panels:
            if panel.id == panel_id:
                return panel
        raise KeyError(panel_id)

    def controller_for_role(self, role: str) -> InstrumentPanelConfig:
        for panel in self.panels:
            if panel.enabled and panel.template == "controller" and panel.role == role:
                return panel
        raise KeyError(role)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """每次运行的数据、事件、仪表状态文件以及落盘策略。"""

    directory: str = "runs"
    data_file_name: str = "experiment.dat"
    event_file_name: str = "events.dat"
    instrument_status_file_name: str = "instrument_status.dat"
    instrument_status_interval_seconds: float = 1.0
    timestamp_epoch: str = "labview_1904"
    flush_every_row: bool = True
    allow_external_paths: bool = False


@dataclass(frozen=True, slots=True)
class AlarmReportingConfig:
    """HTTP 报警发射端配置；令牌只保存引用位置，不直接写入普通配置快照。"""

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:3889/alarm/report"
    token_env: str = "OPENLAB_ALARM_TOKEN"
    token_file: str = ""
    timeout_seconds: float = 3.0
    retry_attempts: int = 3
    retry_delay_seconds: float = 1.0
    queue_size: int = 100
    shutdown_timeout_seconds: float = 2.0
    allow_insecure_http: bool = False


@dataclass(frozen=True, slots=True)
class AlarmConfig:
    """本地事件级别、弹窗策略和远程报警配置。"""

    stability_timeout: Severity = Severity.ERROR
    stale_reading: Severity = Severity.WARNING
    popup_warnings: bool = True
    popup_errors: bool = True
    reporting: AlarmReportingConfig = field(
        default_factory=AlarmReportingConfig
    )


@dataclass(frozen=True, slots=True)
class ModuleConfig:
    """Measurement Module 的目录和操作超时。"""

    directory: str = "modules"
    data_directory: str = "module_data"
    startup_timeout_seconds: float = 10.0
    operation_timeout_seconds: float = 120.0
    shutdown_timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class SystemInstrumentConfig:
    """System Instrument 的发现目录和重连策略。"""

    directory: str = "system_instruments"
    startup_timeout_seconds: float = 10.0
    reconnect_timeout_seconds: float = 60.0
    reconnect_interval_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    """经过完整验证、可在线程间安全共享的应用配置快照。"""

    source_path: Path
    title: str
    ui_scale: float | None
    ui_refresh_ms: int
    poll_interval_seconds: float
    control_poll_interval_seconds: float
    simulation_speed: float
    default_sequence: str
    language: str
    logging: LoggingConfig
    alarms: AlarmConfig
    modules: ModuleConfig
    system_instruments: SystemInstrumentConfig
    instrument_resources: tuple[InstrumentResource, ...]
    instrument_instances: tuple[InstrumentConfig, ...]

    @property
    def panels(self) -> tuple[InstrumentPanelConfig, ...]:
        """Return enabled fixed panels in their global configured order."""

        return tuple(
            sorted(
                (
                    panel
                    for instrument in self.instrument_instances
                    for panel in instrument.panels
                    if panel.enabled
                ),
                key=lambda panel: panel.order if panel.order is not None else -1,
            )
        )

    @property
    def project_root(self) -> Path:
        """返回配置文件所属项目根目录，而不是依赖当前工作目录。"""

        return self.source_path.resolve().parent.parent

    def resolve_project_path(self, value: str | Path) -> Path:
        """把配置中的相对路径稳定解析到项目根目录。"""

        path = Path(value)
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def instrument(self, instrument_id: str) -> InstrumentConfig:
        """按 ID 返回仪表配置；不存在时明确抛出 ``KeyError``。"""

        for item in self.instrument_instances:
            if item.id == instrument_id:
                return item
        raise KeyError(instrument_id)

    def resource(self, resource_id: str) -> InstrumentResource:
        """按稳定 ID 返回一台物理仪表资源。"""

        for item in self.instrument_resources:
            if item.id == resource_id:
                return item
        raise KeyError(resource_id)

    def resource_payload(
        self,
        purpose: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """按用途返回物理仪表资源的 JSON 副本。"""

        if purpose not in {None, "system", "measurement"}:
            raise ValueError(
                "Instrument resource purpose must be system or measurement"
            )

        if purpose == "system":
            return {}
        return {
            item.id: item.public_payload()
            for item in self.instrument_resources
        }


def _severity(value: str, key: str) -> Severity:
    """解析事件等级，并在错误消息中保留具体配置键名。"""

    try:
        return Severity(value.lower())
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be info, warning, or error") from exc


def _ui_scale(value: object) -> float | None:
    """解析手动缩放；``None`` 表示由当前屏幕 DPI 自动计算。"""

    if value is None or (isinstance(value, str) and value.strip().casefold() == "auto"):
        return None
    try:
        scale = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("ui_scale must be 'auto' or a number from 0.75 to 2.0") from exc
    if not 0.75 <= scale <= 2.0:
        raise ConfigurationError("ui_scale must be from 0.75 to 2.0")
    return scale


def _finite_float(value: object, key: str) -> float:
    """解析有限浮点数，统一拒绝 NaN 和正负无穷。"""

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be a finite number") from exc
    if not math.isfinite(result):
        raise ConfigurationError(f"{key} must be a finite number")
    return result


def _positive_float(value: object, key: str) -> float:
    """解析严格大于零的浮点数。"""

    result = _finite_float(value, key)
    if result <= 0:
        raise ConfigurationError(f"{key} must be greater than zero")
    return result


def _positive_int(value: object, key: str) -> int:
    """解析严格大于零的整数。"""

    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be a positive integer") from exc
    if result <= 0:
        raise ConfigurationError(f"{key} must be a positive integer")
    return result


def _nonnegative_float(value: object, key: str) -> float:
    """解析大于等于零的有限浮点数。"""

    result = _finite_float(value, key)
    if result < 0:
        raise ConfigurationError(
            f"{key} must be greater than or equal to zero"
        )
    return result


def _boolean(value: object, key: str) -> bool:
    """只接受 TOML 布尔值，避免字符串 ``"false"`` 被误当成真。"""

    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be true or false")
    return value


def _windows_file_name(value: object, key: str) -> str:
    """验证单个 Windows 文件名，禁止目录穿越、Windows 保留设备名和保留字符。"""

    result = str(value)
    path = Path(result)
    invalid_characters = '<>:"/\\|?*'
    stem = result.split(".", 1)[0].upper()
    if (
        not result
        or result != result.strip()
        or result.rstrip(" .") != result
        or path.is_absolute()
        or path.name != result
        or stem in _WINDOWS_RESERVED_FILE_STEMS
        or any(character in invalid_characters or ord(character) < 32 for character in result)
    ):
        raise ConfigurationError(
            f"{key} must be a plain Windows file name without a directory"
        )
    return result


def load_config(path: str | Path) -> AppConfig:
    """加载 TOML 并返回完整 :class:`AppConfig`。

    该函数是配置的唯一入口。它会拒绝重复仪表 ID、多个同类可控仪表、可控的 monitor、
    无效文件名、越界初始值和不安全的报警地址。调用方可以假定返回对象的
    结构约束已经成立，但实际目标值仍必须由 ``InstrumentManager.validate_target`` 再检查。
    """

    source = Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    allowed_top_level = {
        "application",
        "logging",
        "alarms",
        "modules",
        "system_instruments",
    }
    unknown_top_level = sorted(set(raw) - allowed_top_level)
    if unknown_top_level:
        raise ConfigurationError(
            "Unknown general configuration fields: "
            + ", ".join(unknown_top_level)
        )

    application = raw.get("application", {})
    logging_raw = raw.get("logging", {})
    alarm_raw = raw.get("alarms", {})
    reporting_raw = alarm_raw.get("reporting", {})
    module_raw = raw.get("modules", {})
    system_instrument_raw = raw.get("system_instruments", {})
    for value, label in (
        (application, "application"),
        (logging_raw, "logging"),
        (alarm_raw, "alarms"),
        (module_raw, "modules"),
        (system_instrument_raw, "system_instruments"),
    ):
        if not isinstance(value, dict):
            raise ConfigurationError(f"{label} must be a TOML table")

    unknown_module_fields = sorted(
        set(module_raw)
        - {
            "directory",
            "data_directory",
            "startup_timeout_seconds",
            "operation_timeout_seconds",
            "shutdown_timeout_seconds",
        }
    )
    if unknown_module_fields:
        raise ConfigurationError(
            "Unknown [modules] fields: " + ", ".join(unknown_module_fields)
        )
    unknown_system_instrument_fields = sorted(
        set(system_instrument_raw)
        - {
            "directory",
            "startup_timeout_seconds",
            "reconnect_timeout_seconds",
            "reconnect_interval_seconds",
        }
    )
    if unknown_system_instrument_fields:
        raise ConfigurationError(
            "Unknown [system_instruments] fields: "
            + ", ".join(unknown_system_instrument_fields)
        )

    configs_directory = source.parent
    project_root = configs_directory.parent
    try:
        instrument_resources = load_instrument_resources(
            configs_directory / "visa.resources.toml"
        )
    except InstrumentResourceError as exc:
        raise ConfigurationError(str(exc)) from exc

    instrument_directory = Path(
        str(system_instrument_raw.get("directory", "system_instruments"))
    )
    if not instrument_directory.is_absolute():
        instrument_directory = (project_root / instrument_directory).resolve()
    from .instrument_configuration import load_instrument_instances

    instruments = load_instrument_instances(
        configs_directory,
        instrument_directory,
        project_root,
        instrument_resources,
    )

    ui_refresh_ms = _positive_int(
        application.get("ui_refresh_ms", 200),
        "application.ui_refresh_ms",
    )
    poll_interval_seconds = _positive_float(
        application.get("poll_interval_seconds", 1.0),
        "application.poll_interval_seconds",
    )
    control_poll_interval_seconds = _positive_float(
        application.get("control_poll_interval_seconds", 0.2),
        "application.control_poll_interval_seconds",
    )
    simulation_speed = _positive_float(
        application.get("simulation_speed", 1.0),
        "application.simulation_speed",
    )
    timestamp_epoch = str(
        logging_raw.get("timestamp_epoch", "labview_1904")
    ).strip().casefold()
    if timestamp_epoch not in {"labview_1904", "unix"}:
        raise ConfigurationError(
            "logging.timestamp_epoch must be labview_1904 or unix"
        )
    data_file_name = _windows_file_name(
        logging_raw.get("data_file_name", "experiment.dat"),
        "logging.data_file_name",
    )
    event_file_name = _windows_file_name(
        logging_raw.get("event_file_name", "events.dat"),
        "logging.event_file_name",
    )
    instrument_status_file_name = _windows_file_name(
        logging_raw.get(
            "instrument_status_file_name",
            "instrument_status.dat",
        ),
        "logging.instrument_status_file_name",
    )
    if len({
        data_file_name.casefold(),
        event_file_name.casefold(),
        instrument_status_file_name.casefold(),
    }) != 3:
        raise ConfigurationError(
            "logging data, event, and instrument status file names must be different"
        )
    instrument_status_interval_seconds = _positive_float(
        logging_raw.get(
            "instrument_status_interval_seconds",
            1.0,
        ),
        "logging.instrument_status_interval_seconds",
    )
    if not isinstance(reporting_raw, dict):
        raise ConfigurationError(
            "alarms.reporting must be a TOML table"
        )
    reporting_enabled = _boolean(
        reporting_raw.get("enabled", False),
        "alarms.reporting.enabled",
    )
    reporting_endpoint = str(
        reporting_raw.get(
            "endpoint",
            "http://127.0.0.1:3889/alarm/report",
        )
    ).strip()
    allow_insecure_http = _boolean(
        reporting_raw.get("allow_insecure_http", False),
        "alarms.reporting.allow_insecure_http",
    )
    parsed_endpoint = urlsplit(reporting_endpoint)
    if reporting_enabled:
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.hostname
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
            or parsed_endpoint.fragment
        ):
            raise ConfigurationError(
                "alarms.reporting.endpoint must be an http(s) URL "
                "without credentials or a fragment"
            )
        loopback_host = (
            parsed_endpoint.hostname.casefold() == "localhost"
        )
        try:
            loopback_host = (
                loopback_host
                or ipaddress.ip_address(
                    parsed_endpoint.hostname
                ).is_loopback
            )
        except ValueError:
            pass
        if (
            parsed_endpoint.scheme == "http"
            and not loopback_host
            and not allow_insecure_http
        ):
            raise ConfigurationError(
                "alarms.reporting.endpoint must use HTTPS outside "
                "localhost unless allow_insecure_http=true"
            )
    token_env = str(
        reporting_raw.get(
            "token_env",
            "OPENLAB_ALARM_TOKEN",
        )
    ).strip()
    token_file = str(
        reporting_raw.get("token_file", "")
    ).strip()
    if token_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*",
        token_env,
    ):
        raise ConfigurationError(
            "alarms.reporting.token_env must be an environment "
            "variable name"
        )
    if reporting_enabled and not token_env and not token_file:
        raise ConfigurationError(
            "enabled alarm reporting requires token_env or token_file"
        )

    return AppConfig(
        source_path=source,
        title=str(application.get("title", "OpenLab Control")),
        ui_scale=_ui_scale(application.get("ui_scale", "auto")),
        ui_refresh_ms=ui_refresh_ms,
        poll_interval_seconds=poll_interval_seconds,
        control_poll_interval_seconds=control_poll_interval_seconds,
        simulation_speed=simulation_speed,
        default_sequence=str(application.get("default_sequence", "")),
        language=str(application.get("language", "en_US")),
        logging=LoggingConfig(
            directory=str(logging_raw.get("directory", "runs")),
            data_file_name=data_file_name,
            event_file_name=event_file_name,
            instrument_status_file_name=instrument_status_file_name,
            instrument_status_interval_seconds=(
                instrument_status_interval_seconds
            ),
            timestamp_epoch=timestamp_epoch,
            flush_every_row=bool(logging_raw.get("flush_every_row", True)),
            allow_external_paths=bool(logging_raw.get("allow_external_paths", False)),
        ),
        alarms=AlarmConfig(
            stability_timeout=_severity(str(alarm_raw.get("stability_timeout", "error")), "stability_timeout"),
            stale_reading=_severity(str(alarm_raw.get("stale_reading", "warning")), "stale_reading"),
            popup_warnings=bool(alarm_raw.get("popup_warnings", True)),
            popup_errors=bool(alarm_raw.get("popup_errors", True)),
            reporting=AlarmReportingConfig(
                enabled=reporting_enabled,
                endpoint=reporting_endpoint,
                token_env=token_env,
                token_file=token_file,
                timeout_seconds=_positive_float(
                    reporting_raw.get(
                        "timeout_seconds",
                        3.0,
                    ),
                    "alarms.reporting.timeout_seconds",
                ),
                retry_attempts=_positive_int(
                    reporting_raw.get(
                        "retry_attempts",
                        3,
                    ),
                    "alarms.reporting.retry_attempts",
                ),
                retry_delay_seconds=_nonnegative_float(
                    reporting_raw.get(
                        "retry_delay_seconds",
                        1.0,
                    ),
                    "alarms.reporting.retry_delay_seconds",
                ),
                queue_size=_positive_int(
                    reporting_raw.get("queue_size", 100),
                    "alarms.reporting.queue_size",
                ),
                shutdown_timeout_seconds=_positive_float(
                    reporting_raw.get(
                        "shutdown_timeout_seconds",
                        2.0,
                    ),
                    "alarms.reporting.shutdown_timeout_seconds",
                ),
                allow_insecure_http=allow_insecure_http,
            ),
        ),
        modules=ModuleConfig(
            directory=str(module_raw.get("directory", "modules")),
            data_directory=str(module_raw.get("data_directory", "module_data")),
            startup_timeout_seconds=_positive_float(
                module_raw.get("startup_timeout_seconds", 10.0),
                "modules.startup_timeout_seconds",
            ),
            operation_timeout_seconds=_positive_float(
                module_raw.get("operation_timeout_seconds", 120.0),
                "modules.operation_timeout_seconds",
            ),
            shutdown_timeout_seconds=_positive_float(
                module_raw.get("shutdown_timeout_seconds", 3.0),
                "modules.shutdown_timeout_seconds",
            ),
        ),
        system_instruments=SystemInstrumentConfig(
            directory=str(
                system_instrument_raw.get("directory", "system_instruments")
            ),
            startup_timeout_seconds=_positive_float(
                system_instrument_raw.get("startup_timeout_seconds", 10.0),
                "system_instruments.startup_timeout_seconds",
            ),
            reconnect_timeout_seconds=_positive_float(
                system_instrument_raw.get("reconnect_timeout_seconds", 60.0),
                "system_instruments.reconnect_timeout_seconds",
            ),
            reconnect_interval_seconds=_positive_float(
                system_instrument_raw.get("reconnect_interval_seconds", 2.0),
                "system_instruments.reconnect_interval_seconds",
            ),
        ),
        instrument_resources=instrument_resources,
        instrument_instances=instruments,
    )
