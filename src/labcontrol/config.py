"""严格读取并验证主配置文件。

配置对象在启动时一次性构造为不可变 dataclass，之后由 UI、运行时和插件服务共同读取。
所有影响真实仪表安全的值（上下限、速率、超时、主设备角色）都在进入运行时前验证；不能
依赖某个对话框临时校验，因为无界面模式和第三方调用同样会使用这些配置。
"""

from __future__ import annotations

import math
import ipaddress
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import DeviceKind, DeviceRole, Severity


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
    """框架独立稳定性判定参数，单位沿用对应设备的显示单位。"""

    tolerance: float
    max_slope_per_minute: float
    dwell_seconds: float
    timeout_seconds: float
    window_seconds: float
    stale_after_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """一个逻辑设备实例的驱动选择、角色、限制和超时。

    ``plugin`` 可以指向内置 ``module:class``，也可以是外部 Device Plugin 的清单 ID。
    ``control_enabled`` 是运行时的最终授权边界：Monitor 和只读设备不能因 UI 操作而绕过它。
    ``extras`` 原样传递给具体驱动，便于更换仪表时只改 TOML。
    """

    id: str
    display_name: str
    kind: DeviceKind
    plugin: str
    role: DeviceRole = DeviceRole.SECONDARY
    control_enabled: bool = False
    unit: str = ""
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


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """每次运行的数据、事件、设备状态文件以及落盘策略。"""

    directory: str = "runs"
    data_file_name: str = "experiment.dat"
    event_file_name: str = "events.dat"
    device_status_file_name: str = "device_status.dat"
    device_status_interval_seconds: float = 1.0
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
    """Measurement Module 的目录、隔离运行时和操作超时。"""

    directory: str = "modules"
    data_directory: str = "module_data"
    shared_wheels_directory: str = "wheels"
    python_executable: str = ""
    runtime_directory: str = "plugin_runtime"
    startup_timeout_seconds: float = 10.0
    operation_timeout_seconds: float = 120.0
    shutdown_timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class PluginConfig:
    """Device Plugin 的发现目录、信任状态、依赖目录和重连策略。"""

    device_directory: str = "device_plugins"
    state_directory: str = "plugin_state"
    runtime_directory: str = "plugin_runtime"
    shared_wheels_directory: str = "wheels"
    python_executable: str = ""
    device_startup_timeout_seconds: float = 10.0
    device_reconnect_timeout_seconds: float = 60.0
    device_reconnect_interval_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    """经过完整验证、可在线程间安全共享的应用配置快照。"""

    source_path: Path
    title: str
    ui_scale: float | None
    ui_refresh_ms: int
    poll_interval_seconds: float
    simulation_speed: float
    default_sequence: str
    language: str
    logging: LoggingConfig
    alarms: AlarmConfig
    modules: ModuleConfig
    plugins: PluginConfig
    abort_temperature: str
    abort_field: str
    devices: tuple[DeviceConfig, ...]

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

    def device(self, device_id: str) -> DeviceConfig:
        """按 ID 返回设备配置；不存在时明确抛出 ``KeyError``。"""

        for item in self.devices:
            if item.id == device_id:
                return item
        raise KeyError(device_id)


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
    """验证单个 Windows 文件名，禁止目录穿越、设备名和保留字符。"""

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


def _device_identifier(value: object) -> str:
    """验证可安全用于映射键和事件上下文的设备 ID。"""

    result = str(value)
    if (
        not result
        or result != result.strip()
        or any(not character.isprintable() for character in result)
    ):
        raise ConfigurationError(
            "Device id must be non-empty printable text without surrounding whitespace"
        )
    return result


def _device_config(raw: dict[str, Any]) -> DeviceConfig:
    """把一个 ``[[devices]]`` 表转换为经过角色与安全限制校验的配置。"""

    required = ("id", "display_name", "kind", "plugin")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigurationError(f"Device configuration is missing fields: {', '.join(missing)}")
    try:
        kind = DeviceKind(str(raw["kind"]).lower())
    except ValueError as exc:
        raise ConfigurationError(f"Unknown device kind: {raw['kind']}") from exc

    device_id = _device_identifier(raw["id"])
    prefix = f"Device {device_id}"
    default_role = (
        DeviceRole.MONITOR
        if kind is DeviceKind.MONITOR
        else DeviceRole.SECONDARY
    )
    try:
        role = DeviceRole(str(raw.get("role", default_role.value)).strip().casefold())
    except ValueError as exc:
        raise ConfigurationError(
            f"{prefix} role must be primary, secondary, or monitor"
        ) from exc
    control_enabled = _boolean(
        raw.get("control_enabled", role is DeviceRole.PRIMARY),
        f"{prefix} control_enabled",
    )
    if kind is DeviceKind.MONITOR:
        if role is not DeviceRole.MONITOR:
            raise ConfigurationError(
                f"{prefix} kind=monitor requires role=monitor"
            )
        if control_enabled:
            raise ConfigurationError(
                f"{prefix} monitor devices cannot enable control"
            )
    else:
        if role is DeviceRole.MONITOR:
            raise ConfigurationError(
                f"{prefix} temperature/field devices use primary or secondary role"
            )
        if role is DeviceRole.PRIMARY and not control_enabled:
            raise ConfigurationError(
                f"{prefix} primary devices must enable control"
            )
    initial_value = _finite_float(
        raw.get("initial_value", 0.0),
        f"{prefix} initial_value",
    )
    default_rate = _finite_float(
        raw.get("default_rate_per_minute", 1.0),
        f"{prefix} default_rate_per_minute",
    )
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        min_value = _finite_float(
            raw.get("min_value", float("-inf")),
            f"{prefix} min_value",
        )
        max_value = _finite_float(
            raw.get("max_value", float("inf")),
            f"{prefix} max_value",
        )
        max_rate = _finite_float(
            raw.get("max_rate_per_minute", float("inf")),
            f"{prefix} max_rate_per_minute",
        )
    else:
        min_value = float(raw.get("min_value", float("-inf")))
        max_value = float(raw.get("max_value", float("inf")))
        max_rate = float(raw.get("max_rate_per_minute", float("inf")))
    stale_after = _positive_float(
        raw.get("stale_after_seconds", 3.0),
        f"{prefix} stale_after_seconds",
    )
    operation_timeout = _positive_float(
        raw.get("operation_timeout_seconds", 10.0),
        f"{prefix} operation_timeout_seconds",
    )
    shutdown_timeout = _positive_float(
        raw.get("shutdown_timeout_seconds", 3.0),
        f"{prefix} shutdown_timeout_seconds",
    )

    stability = None
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        tolerance = _finite_float(
            raw.get("stability_tolerance", 0.01),
            f"{prefix} stability_tolerance",
        )
        maximum_slope = _finite_float(
            raw.get("stability_max_slope_per_minute", 0.01),
            f"{prefix} stability_max_slope_per_minute",
        )
        dwell = _finite_float(
            raw.get("stability_dwell_seconds", 5.0),
            f"{prefix} stability_dwell_seconds",
        )
        if tolerance < 0 or maximum_slope < 0 or dwell < 0:
            raise ConfigurationError(
                f"{prefix} stability tolerance, slope, and dwell must not be negative"
            )
        stability = StabilityConfig(
            tolerance=tolerance,
            max_slope_per_minute=maximum_slope,
            dwell_seconds=dwell,
            timeout_seconds=_positive_float(
                raw.get("stability_timeout_seconds", 1800.0),
                f"{prefix} stability_timeout_seconds",
            ),
            window_seconds=_positive_float(
                raw.get("stability_window_seconds", 5.0),
                f"{prefix} stability_window_seconds",
            ),
            stale_after_seconds=stale_after,
        )

    known = {
        "id", "display_name", "kind", "plugin", "role", "control_enabled",
        "unit", "initial_value",
        "default_rate_per_minute", "min_value", "max_value",
        "max_rate_per_minute", "stability_tolerance",
        "stability_max_slope_per_minute", "stability_dwell_seconds",
        "stability_timeout_seconds", "stability_window_seconds",
        "stale_after_seconds", "operation_timeout_seconds",
        "shutdown_timeout_seconds",
    }
    device = DeviceConfig(
        id=device_id,
        display_name=str(raw["display_name"]),
        kind=kind,
        plugin=str(raw["plugin"]),
        role=role,
        control_enabled=control_enabled,
        unit=str(raw.get("unit", "")),
        initial_value=initial_value,
        default_rate_per_minute=default_rate,
        min_value=min_value,
        max_value=max_value,
        max_rate_per_minute=max_rate,
        stale_after_seconds=stale_after,
        operation_timeout_seconds=operation_timeout,
        shutdown_timeout_seconds=shutdown_timeout,
        stability=stability,
        extras={key: value for key, value in raw.items() if key not in known},
    )
    if kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        if device.min_value >= device.max_value:
            raise ConfigurationError(f"Device {device.id}: min_value must be less than max_value")
        if device.default_rate_per_minute <= 0 or device.max_rate_per_minute <= 0:
            raise ConfigurationError(f"Device {device.id}: rates must be greater than zero")
        if device.default_rate_per_minute > device.max_rate_per_minute:
            raise ConfigurationError(
                f"Device {device.id}: default rate must not exceed max_rate_per_minute"
            )
        if not device.min_value <= device.initial_value <= device.max_value:
            raise ConfigurationError(
                f"Device {device.id}: initial_value must be within min_value and max_value"
            )
    return device


def load_config(path: str | Path) -> AppConfig:
    """加载 TOML 并返回完整 :class:`AppConfig`。

    该函数是配置的唯一入口。它会拒绝重复设备 ID、多个同类主控设备、不可控的 primary、
    可控的 monitor、无效文件名、越界初始值和不安全的报警地址。调用方可以假定返回对象的
    结构约束已经成立，但实际目标值仍必须由 ``DeviceManager.validate_target`` 再检查。
    """

    source = Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    application = raw.get("application", {})
    logging_raw = raw.get("logging", {})
    alarm_raw = raw.get("alarms", {})
    reporting_raw = alarm_raw.get("reporting", {})
    abort_raw = raw.get("abort", {})
    module_raw = raw.get("modules", {})
    plugin_raw = raw.get("plugins", {})
    raw_devices = list(raw.get("devices", []))
    parsed_devices = [_device_config(item) for item in raw_devices]
    # Compatibility for pre-0.11 configurations: when no role is declared for
    # a controlled quantity, the first device of that kind remains primary.
    for kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        matching_indices = [
            index
            for index, device in enumerate(parsed_devices)
            if device.kind is kind
        ]
        if not matching_indices:
            continue
        explicit_role = any(
            "role" in raw_devices[index]
            for index in matching_indices
        )
        has_primary = any(
            parsed_devices[index].role is DeviceRole.PRIMARY
            for index in matching_indices
        )
        if not explicit_role and not has_primary:
            first = matching_indices[0]
            parsed_devices[first] = replace(
                parsed_devices[first],
                role=DeviceRole.PRIMARY,
                control_enabled=True,
            )
    devices = tuple(parsed_devices)
    if not devices:
        raise ConfigurationError("Configuration must contain at least one [[devices]] entry")
    ids = [device.id for device in devices]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Device IDs must be unique")
    for kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
        primary = [
            device.id
            for device in devices
            if device.kind is kind and device.role is DeviceRole.PRIMARY
        ]
        if len(primary) > 1:
            raise ConfigurationError(
                f"Only one primary {kind.value} device is allowed: "
                + ", ".join(primary)
            )

    ui_refresh_ms = _positive_int(
        application.get("ui_refresh_ms", 200),
        "application.ui_refresh_ms",
    )
    poll_interval_seconds = _positive_float(
        application.get("poll_interval_seconds", 1.0),
        "application.poll_interval_seconds",
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
    abort_temperature = str(
        abort_raw.get("temperature", "hold_current")
    ).strip().casefold()
    abort_field = str(
        abort_raw.get("field", "hold_current")
    ).strip().casefold()
    if abort_temperature != "hold_current" or abort_field != "hold_current":
        raise ConfigurationError(
            "abort.temperature and abort.field currently support only hold_current"
        )
    data_file_name = _windows_file_name(
        logging_raw.get("data_file_name", "experiment.dat"),
        "logging.data_file_name",
    )
    event_file_name = _windows_file_name(
        logging_raw.get("event_file_name", "events.dat"),
        "logging.event_file_name",
    )
    device_status_file_name = _windows_file_name(
        logging_raw.get(
            "device_status_file_name",
            "device_status.dat",
        ),
        "logging.device_status_file_name",
    )
    if len({
        data_file_name.casefold(),
        event_file_name.casefold(),
        device_status_file_name.casefold(),
    }) != 3:
        raise ConfigurationError(
            "logging data, event, and device status file names must be different"
        )
    device_status_interval_seconds = _positive_float(
        logging_raw.get(
            "device_status_interval_seconds",
            1.0,
        ),
        "logging.device_status_interval_seconds",
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
        simulation_speed=simulation_speed,
        default_sequence=str(application.get("default_sequence", "")),
        language=str(application.get("language", "en_US")),
        logging=LoggingConfig(
            directory=str(logging_raw.get("directory", "runs")),
            data_file_name=data_file_name,
            event_file_name=event_file_name,
            device_status_file_name=device_status_file_name,
            device_status_interval_seconds=(
                device_status_interval_seconds
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
            shared_wheels_directory=str(module_raw.get("shared_wheels_directory", "wheels")),
            python_executable=str(module_raw.get("python_executable", "")),
            runtime_directory=str(
                module_raw.get(
                    "runtime_directory",
                    str(
                        Path(
                            str(
                                module_raw.get(
                                    "site_packages_directory",
                                    "plugin_runtime/site-packages",
                                )
                            )
                        ).parent
                    ),
                )
            ),
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
        plugins=PluginConfig(
            device_directory=str(
                plugin_raw.get("device_directory", "device_plugins")
            ),
            state_directory=str(
                plugin_raw.get("state_directory", "plugin_state")
            ),
            runtime_directory=str(
                plugin_raw.get("runtime_directory", "plugin_runtime")
            ),
            shared_wheels_directory=str(
                plugin_raw.get("shared_wheels_directory", "wheels")
            ),
            python_executable=str(plugin_raw.get("python_executable", "")),
            device_startup_timeout_seconds=_positive_float(
                plugin_raw.get("device_startup_timeout_seconds", 10.0),
                "plugins.device_startup_timeout_seconds",
            ),
            device_reconnect_timeout_seconds=_positive_float(
                plugin_raw.get("device_reconnect_timeout_seconds", 60.0),
                "plugins.device_reconnect_timeout_seconds",
            ),
            device_reconnect_interval_seconds=_positive_float(
                plugin_raw.get("device_reconnect_interval_seconds", 2.0),
                "plugins.device_reconnect_interval_seconds",
            ),
        ),
        abort_temperature=abort_temperature,
        abort_field=abort_field,
        devices=devices,
    )
