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
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

from .instrument_resources import (
    InstrumentResource,
    InstrumentResourceError,
    load_instrument_resources,
)
from .models import InstrumentKind, Severity

if TYPE_CHECKING:
    from .instruments.manifest import SystemInstrumentDescriptor


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
class InstrumentConfig:
    """一个逻辑系统仪表实例的后端选择、读数、限制和超时。

    外部 ``backend`` 从物理资源选择自动得到；只有内置模拟仪表在站点配置中直接声明入口。
    ``control_enabled`` 是运行时的最终授权边界：只读仪表不能因 UI 操作而绕过它。
    ``extras`` 原样传递给具体驱动，便于更换仪表时只改 TOML。
    """

    id: str
    display_name: str
    kind: InstrumentKind
    backend: str
    panel_template: str
    resource_id: str = ""
    address: str = ""
    control_enabled: bool = False
    unit: str = ""
    main_reading: str = "value"
    auxiliary_readings: tuple[str, ...] = ()
    readings: tuple[InstrumentReadingConfig, ...] = ()
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
    """Measurement Module 的目录、隔离运行时和操作超时。"""

    directory: str = "modules"
    data_directory: str = "module_data"
    state_directory: str = "trust_state"
    shared_wheels_directory: str = "wheels"
    python_executable: str = ""
    runtime_directory: str = "runtime_packages"
    startup_timeout_seconds: float = 10.0
    operation_timeout_seconds: float = 120.0
    shutdown_timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class SystemInstrumentConfig:
    """System Instrument 的发现目录、信任状态、依赖目录和重连策略。"""

    directory: str = "system_instruments"
    state_directory: str = "trust_state"
    runtime_directory: str = "runtime_packages"
    shared_wheels_directory: str = "wheels"
    python_executable: str = ""
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
    instruments: tuple[InstrumentConfig, ...]

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

        for item in self.instruments:
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

        return {
            item.id: item.public_payload()
            for item in self.instrument_resources
            if purpose is None or item.purpose == purpose
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


def _instrument_identifier(value: object) -> str:
    """验证可安全用于映射键和事件上下文的仪表 ID。"""

    result = str(value)
    if (
        not result
        or result != result.strip()
        or any(not character.isprintable() for character in result)
    ):
        raise ConfigurationError(
            "Instrument id must be non-empty printable text without surrounding whitespace"
        )
    return result


def _instrument_config(
    raw: dict[str, Any],
    resources: dict[str, InstrumentResource],
    descriptors: dict[str, "SystemInstrumentDescriptor"],
) -> InstrumentConfig:
    """把一个 ``[[instruments]]`` 表转换为经过安全限制校验的配置。"""

    required = ("id", "display_name", "kind")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigurationError(f"Instrument configuration is missing fields: {', '.join(missing)}")
    try:
        kind = InstrumentKind(str(raw["kind"]).lower())
    except ValueError as exc:
        raise ConfigurationError(f"Unknown instrument kind: {raw['kind']}") from exc

    instrument_id = _instrument_identifier(raw["id"])
    prefix = f"Instrument {instrument_id}"
    if "role" in raw:
        raise ConfigurationError(
            f"{prefix} role is no longer used; control_enabled alone selects the controller"
        )
    derived_reading_fields = sorted(
        set(raw)
        & {
            "auxiliary_readings",
            "main_reading",
            "monitor_readings",
            "primary_reading",
            "reading_labels",
            "readings",
        }
    )
    if derived_reading_fields:
        raise ConfigurationError(
            f"{prefix} reading metadata comes from its System Instrument manifest; "
            f"remove: {', '.join(derived_reading_fields)}"
        )
    control_enabled = _boolean(
        raw.get("control_enabled", False),
        f"{prefix} control_enabled",
    )
    if kind is InstrumentKind.MONITOR:
        if control_enabled:
            raise ConfigurationError(
                f"{prefix} monitor instruments cannot enable control"
            )
    default_rate = _finite_float(
        raw.get("default_rate_per_minute", 1.0),
        f"{prefix} default_rate_per_minute",
    )
    if kind in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD):
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
    if kind in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD):
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

    if "address" in raw:
        raise ConfigurationError(
            f"{prefix} must store its physical address in site [[resources]] "
            "and select it with resource"
        )
    resource_id = str(raw.get("resource", "")).strip()
    if resource_id:
        if "backend" in raw:
            raise ConfigurationError(
                f"{prefix} backend is selected by its resource and must not be repeated"
            )
        if "unit" in raw:
            raise ConfigurationError(
                f"{prefix} unit is declared by its System Instrument manifest"
            )
        if "initial_value" in raw:
            raise ConfigurationError(
                f"{prefix} initial_value is only used by built-in simulators"
            )
        resource = resources.get(resource_id)
        if resource is None:
            raise ConfigurationError(
                f"{prefix} selects unknown resource {resource_id!r}"
            )
        if resource.purpose != "system":
            raise ConfigurationError(
                f"{prefix} resource {resource_id!r} is reserved for a Measurement Module"
            )
        backend = resource.system_instrument
        descriptor = descriptors.get(backend)
        if descriptor is None:
            raise ConfigurationError(
                f"{prefix} selects missing System Instrument {backend!r}"
            )
        if not descriptor.can_load:
            raise ConfigurationError(
                f"{prefix} selects invalid System Instrument {backend!r}: {descriptor.error}"
            )
        if kind not in descriptor.kinds:
            raise ConfigurationError(
                f"{prefix} System Instrument {backend!r} does not support {kind.value}"
            )
        unsupported_auxiliary = set(resource.auxiliary_readings) - set(
            descriptor.auxiliary_readings
        )
        if unsupported_auxiliary:
            raise ConfigurationError(
                f"{prefix} resource selects unsupported auxiliary readings: "
                + ", ".join(sorted(unsupported_auxiliary))
            )
        selected_keys = (descriptor.main_reading, *resource.auxiliary_readings)
        readings = tuple(
            InstrumentReadingConfig(
                key=metadata.key,
                display_name=metadata.label,
                unit=metadata.unit,
                decimals=metadata.decimals,
            )
            for metadata in (descriptor.reading(key) for key in selected_keys)
        )
        main_reading = descriptor.main_reading
        auxiliary_readings = resource.auxiliary_readings
        panel_template = descriptor.panel_template
        unit = readings[0].unit
        address = resource.address
        initial_value = 0.0
    else:
        if "backend" not in raw:
            raise ConfigurationError(
                f"{prefix} requires resource for a real instrument or backend for a built-in simulator"
            )
        backend = str(raw["backend"]).strip()
        if ":" not in backend:
            raise ConfigurationError(
                f"{prefix} external System Instruments must be selected through resource"
            )
        unit = str(raw.get("unit", ""))
        main_reading = "value"
        auxiliary_readings = ()
        panel_template = (
            "controller"
            if kind in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD)
            else "readout"
        )
        readings = (
            InstrumentReadingConfig(
                key=main_reading,
                display_name=str(raw["display_name"]),
                unit=unit,
            ),
        )
        address = ""
        initial_value = _finite_float(
            raw.get("initial_value", 0.0),
            f"{prefix} initial_value",
        )

    known = {
        "id", "display_name", "kind", "backend", "control_enabled",
        "resource",
        "unit", "initial_value",
        "default_rate_per_minute", "min_value", "max_value",
        "max_rate_per_minute", "stability_tolerance",
        "stability_max_slope_per_minute", "stability_dwell_seconds",
        "stability_timeout_seconds", "stability_window_seconds",
        "stale_after_seconds", "operation_timeout_seconds",
        "shutdown_timeout_seconds",
    }
    instrument = InstrumentConfig(
        id=instrument_id,
        display_name=str(raw["display_name"]),
        kind=kind,
        backend=backend,
        panel_template=panel_template,
        resource_id=resource_id,
        address=address,
        control_enabled=control_enabled,
        unit=unit,
        main_reading=main_reading,
        auxiliary_readings=auxiliary_readings,
        readings=readings,
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
    if kind in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD):
        if instrument.control_enabled and instrument.panel_template != "controller":
            raise ConfigurationError(
                f"Instrument {instrument.id}: control_enabled requires the controller panel template"
            )
        if instrument.min_value >= instrument.max_value:
            raise ConfigurationError(f"Instrument {instrument.id}: min_value must be less than max_value")
        if instrument.default_rate_per_minute <= 0 or instrument.max_rate_per_minute <= 0:
            raise ConfigurationError(f"Instrument {instrument.id}: rates must be greater than zero")
        if instrument.default_rate_per_minute > instrument.max_rate_per_minute:
            raise ConfigurationError(
                f"Instrument {instrument.id}: default rate must not exceed max_rate_per_minute"
            )
        if (
            not resource_id
            and not (
                instrument.min_value
                <= instrument.initial_value
                <= instrument.max_value
            )
        ):
            raise ConfigurationError(
                f"Instrument {instrument.id}: initial_value must be within min_value and max_value"
            )
    return instrument


def load_config(path: str | Path) -> AppConfig:
    """加载 TOML 并返回完整 :class:`AppConfig`。

    该函数是配置的唯一入口。它会拒绝重复仪表 ID、多个同类可控仪表、可控的 monitor、
    无效文件名、越界初始值和不安全的报警地址。调用方可以假定返回对象的
    结构约束已经成立，但实际目标值仍必须由 ``InstrumentManager.validate_target`` 再检查。
    """

    source = Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    if "abort" in raw:
        raise ConfigurationError(
            "[abort] is not supported; SEQ exit does not control System Instruments"
        )

    application = raw.get("application", {})
    logging_raw = raw.get("logging", {})
    alarm_raw = raw.get("alarms", {})
    reporting_raw = alarm_raw.get("reporting", {})
    module_raw = raw.get("modules", {})
    system_instrument_raw = raw.get("system_instruments", {})
    if "resource_file" in system_instrument_raw:
        raise ConfigurationError(
            "system_instruments.resource_file is no longer used; keep "
            "[[resources]] in the same site configuration"
        )
    try:
        instrument_resources = load_instrument_resources(source)
    except InstrumentResourceError as exc:
        raise ConfigurationError(str(exc)) from exc
    resources_by_id = {
        item.id: item
        for item in instrument_resources
    }
    from .instruments.manifest import load_instrument_manifest

    instrument_directory_value = str(
        system_instrument_raw.get("directory", "system_instruments")
    )
    instrument_directory = Path(instrument_directory_value)
    if not instrument_directory.is_absolute():
        instrument_directory = source.parent.parent / instrument_directory
    descriptors: dict[str, SystemInstrumentDescriptor] = {}
    if instrument_directory.is_dir():
        for instrument_path in sorted(
            instrument_directory.iterdir(),
            key=lambda item: item.name.casefold(),
        ):
            if not instrument_path.is_dir() or not (
                instrument_path / "instrument.toml"
            ).is_file():
                continue
            descriptor = load_instrument_manifest(instrument_path)
            if descriptor.id in descriptors:
                raise ConfigurationError(
                    f"Duplicate System Instrument id: {descriptor.id}"
                )
            descriptors[descriptor.id] = descriptor
    raw_instruments = list(raw.get("instruments", []))
    parsed_instruments = [
        _instrument_config(item, resources_by_id, descriptors)
        for item in raw_instruments
    ]
    instruments = tuple(parsed_instruments)
    if not instruments:
        raise ConfigurationError("Configuration must contain at least one [[instruments]] entry")
    ids = [instrument.id for instrument in instruments]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("Instrument IDs must be unique")
    selected_resources = [
        instrument.resource_id
        for instrument in instruments
        if instrument.resource_id
    ]
    if len(selected_resources) != len(set(selected_resources)):
        raise ConfigurationError(
            "A physical instrument resource can be selected by only one "
            "[[instruments]] entry; return its additional readings as auxiliary values"
        )
    for kind in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD):
        controllers = [
            instrument.id
            for instrument in instruments
            if instrument.kind is kind and instrument.control_enabled
        ]
        if len(controllers) > 1:
            raise ConfigurationError(
                f"Only one controllable {kind.value} instrument is allowed: "
                + ", ".join(controllers)
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
            state_directory=str(module_raw.get("state_directory", "trust_state")),
            shared_wheels_directory=str(module_raw.get("shared_wheels_directory", "wheels")),
            python_executable=str(module_raw.get("python_executable", "")),
            runtime_directory=str(
                module_raw.get("runtime_directory", "runtime_packages")
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
        system_instruments=SystemInstrumentConfig(
            directory=str(
                system_instrument_raw.get("directory", "system_instruments")
            ),
            state_directory=str(
                system_instrument_raw.get("state_directory", "trust_state")
            ),
            runtime_directory=str(
                system_instrument_raw.get("runtime_directory", "runtime_packages")
            ),
            shared_wheels_directory=str(
                system_instrument_raw.get("shared_wheels_directory", "wheels")
            ),
            python_executable=str(system_instrument_raw.get("python_executable", "")),
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
        instruments=instruments,
    )
