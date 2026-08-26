"""一次 SEQ 运行的目录、快照、DAT 数据、仪表状态和事件日志写入。

运行开始即复制规范化 SEQ、完整现场配置、各模块期望设置和启动状态。DAT 列由全部仪表与已启用
模块清单一次构造；一次 ``T Measure`` 按逻辑通道槽位写多行，同一槽位中各模块的结果
合并到该槽位对应的一行，未参与该槽位的模块列留空。
仪表状态按独立节流周期写入固定宽表，不受 Measure 数量影响。追加已有 DAT 前必须验证完整
列结构一致，防止动态列变化造成静默错位。
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from . import __version__
from .config import AppConfig
from .events import EventManager
from .formatting import control_decimals, fixed_number
from .models import InstrumentKind, InstrumentMetric, InstrumentSnapshot, EventNotice, Severity
from .measurement.manifest import ModuleDescriptor
from .measurement.settings import save_settings


LABVIEW_UNIX_OFFSET_SECONDS = 2_082_844_800.0
DATAFILE_MODES = frozenset({"open", "open|create", "create"})


@dataclass(frozen=True, slots=True)
class RunPaths:
    """一份运行产生的全部规范路径。"""

    directory: Path
    data_file: Path
    event_file: Path
    instrument_status_file: Path
    sequence_snapshot: Path
    configuration_snapshot: Path
    module_settings_directory: Path
    raw_data_directory: Path


class DatRunLogger:
    """为一条 SEQ 写入模板兼容的 DAT、事件文件和可复现实验快照。"""

    def __init__(self, config: AppConfig, events: EventManager) -> None:
        """订阅事件并初始化惰性文件句柄；此时尚不创建运行目录。"""

        self.config = config
        self.events = events
        self.paths: RunPaths | None = None
        self._started_monotonic = 0.0
        self._data_handle: TextIO | None = None
        self._data_writer: csv.writer | None = None
        self._event_handle: TextIO | None = None
        self._event_writer: csv.writer | None = None
        self._instrument_status_handle: TextIO | None = None
        self._instrument_status_writer: csv.writer | None = None
        self._last_instrument_status_monotonic: float | None = None
        self._columns: list[str] = []
        self._data_file_initialized = False
        self._module_descriptors: tuple[ModuleDescriptor, ...] = ()
        self._instrument_metric_schemas: dict[
            str,
            tuple[tuple[str, InstrumentMetric], ...],
        ] = {}
        self._raw_handles: dict[tuple[Path, str], TextIO] = {}
        self._raw_writers: dict[tuple[Path, str], csv.writer] = {}
        self._pending_events: list[EventNotice] = []
        self.events.subscribe(self.on_event)

    def open_run(
        self,
        sequence_name: str,
        sequence_text: str,
        module_descriptors: tuple[ModuleDescriptor, ...] = (),
        module_settings: Mapping[str, Mapping[str, Any]] | None = None,
        module_status: Mapping[str, Mapping[str, Any]] | None = None,
        instrument_snapshots: Mapping[str, InstrumentSnapshot] | None = None,
    ) -> RunPaths:
        """创建唯一运行目录并先写入配置、SEQ 与模块启动快照。

        目录名包含秒级时间；同一秒重复运行通过递增后缀避免覆盖。事件可能在目录建立前产生，
        因此先暂存在内存，事件文件打开后按原顺序补写。
        """

        root = self.config.resolve_project_path(self.config.logging.directory)
        root.mkdir(parents=True, exist_ok=True)
        stem = Path(sequence_name).stem or "sequence"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_stem = self._safe_name(stem)
        counter = 0
        while True:
            suffix = "" if counter == 0 else f"_{counter:02d}"
            directory = root / f"{stamp}_{safe_stem}{suffix}"
            try:
                directory.mkdir()
            except FileExistsError:
                counter += 1
                continue
            break
        data_file = directory / self.config.logging.data_file_name
        event_file = directory / self.config.logging.event_file_name
        instrument_status_file = (
            directory
            / self.config.logging.instrument_status_file_name
        )
        sequence_snapshot = directory / "sequence.seq"
        config_snapshot = directory / "configuration"
        config_snapshot.mkdir()
        module_settings_directory = directory / "module_settings"
        module_settings_directory.mkdir()
        raw_data_directory = directory / "rawdata"
        sequence_snapshot.write_text(sequence_text, encoding="utf-8", newline="\n")
        shutil.copy2(self.config.source_path, config_snapshot / "general.toml")
        source_configs = self.config.source_path.parent
        visa_resources = source_configs / "visa.resources.toml"
        if visa_resources.is_file():
            shutil.copy2(visa_resources, config_snapshot / visa_resources.name)
        for name in ("instruments", "pid"):
            source_directory = source_configs / name
            if not source_directory.is_dir():
                continue
            destination_directory = config_snapshot / name
            destination_directory.mkdir()
            for source_file in sorted(source_directory.glob("*.toml")):
                shutil.copy2(source_file, destination_directory / source_file.name)
        self._module_descriptors = tuple(module_descriptors)
        initial_snapshots = instrument_snapshots or {}
        self._instrument_metric_schemas = {}
        for instrument in self.config.instrument_instances:
            snapshot = initial_snapshots.get(instrument.id)
            self._instrument_metric_schemas[instrument.id] = tuple(
                (
                    metric_key,
                    InstrumentMetric(
                        display_name=metric.display_name,
                        value=None,
                        unit=metric.unit,
                        decimals=metric.decimals,
                    ),
                )
                for metric_key, metric in (
                    () if snapshot is None else snapshot.metrics.items()
                )
            )
        desired = module_settings or {}
        actual = module_status or {}
        for descriptor in self._module_descriptors:
            save_settings(
                module_settings_directory / f"{descriptor.id}.settings.toml",
                dict(desired.get(descriptor.id, {})),
            )
            (module_settings_directory / f"{descriptor.id}.status-at-start.json").write_text(
                json.dumps(dict(actual.get(descriptor.id, {})), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        self.paths = RunPaths(
            directory=directory,
            data_file=data_file,
            event_file=event_file,
            instrument_status_file=instrument_status_file,
            sequence_snapshot=sequence_snapshot,
            configuration_snapshot=config_snapshot,
            module_settings_directory=module_settings_directory,
            raw_data_directory=raw_data_directory,
        )
        self._data_file_initialized = False
        self._started_monotonic = time.monotonic()
        self._last_instrument_status_monotonic = None
        self._open_event_file(event_file)
        self._open_instrument_status_file(instrument_status_file)
        for notice in self._pending_events:
            self._write_event(notice)
        self._pending_events.clear()
        return self.paths

    def set_datafile(
        self,
        requested: str,
        mode: str = "open|create",
        *,
        allow_external: bool = False,
    ) -> Path:
        """执行 ``Set Datafile``，限制路径范围并按 open/create 语义打开。

        默认只允许运行目录内文件；绝对路径和 ``..`` 逃逸会被重定向并产生 Warning。只有主
        配置或命令明确允许外部路径时才可写到任意位置。
        """

        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        normalized_mode = self._normalize_datafile_mode(mode)
        path = Path(requested)
        external_allowed = allow_external or self.config.logging.allow_external_paths
        if path.is_absolute() and not external_allowed:
            destination = self.paths.directory / path.name
            self.events.report(
                Severity.WARNING,
                "logging",
                "DATAFILE_RELOCATED",
                f"External data path redirected to the run directory: {destination.name}",
                str(path),
            )
        elif path.is_absolute():
            destination = path
        else:
            destination = (self.paths.directory / path).resolve()
            if self.paths.directory.resolve() not in destination.parents and destination != self.paths.directory.resolve():
                destination = self.paths.directory / path.name
                self.events.report(
                    Severity.WARNING,
                    "logging",
                    "DATAFILE_RELOCATED",
                    f"Out-of-scope data path redirected to the run directory: {destination.name}",
                    requested,
                )
        if not destination.name:
            raise ValueError("Data file path must name a file")
        destination_resolved = destination.resolve()
        module_settings_resolved = (
            self.paths.module_settings_directory.resolve()
        )
        raw_data_resolved = self.paths.raw_data_directory.resolve()
        configuration_resolved = self.paths.configuration_snapshot.resolve()
        reserved_paths = {
            self.paths.directory.resolve(),
            self.paths.event_file.resolve(),
            self.paths.instrument_status_file.resolve(),
            self.paths.sequence_snapshot.resolve(),
            configuration_resolved,
            module_settings_resolved,
            raw_data_resolved,
        }
        if (
            destination_resolved in reserved_paths
            or configuration_resolved in destination_resolved.parents
            or module_settings_resolved
            in destination_resolved.parents
            or raw_data_resolved in destination_resolved.parents
        ):
            raise ValueError(
                "Data file path collides with a reserved run artifact"
            )
        columns = self._build_columns()
        append = self._validate_data_file_plan(
            destination,
            normalized_mode,
            columns,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous_data_file = self.paths.data_file.resolve()
        self._close_raw_datafile(previous_data_file)
        if not append:
            # ``create`` 会截断正式 DAT，因此属于旧内容的 rawdata 也必须在写入
            # 新一代数据前删除；否则行号会从旧实验继续累计，失去一一对应关系。
            self._remove_raw_sidecars(destination_resolved)
        self._close_data_file()
        self._open_data_file(
            destination,
            normalized_mode,
            columns=columns,
            append=append,
        )
        self.paths = RunPaths(
            directory=self.paths.directory,
            data_file=destination,
            event_file=self.paths.event_file,
            instrument_status_file=self.paths.instrument_status_file,
            sequence_snapshot=self.paths.sequence_snapshot,
            configuration_snapshot=self.paths.configuration_snapshot,
            module_settings_directory=self.paths.module_settings_directory,
            raw_data_directory=self.paths.raw_data_directory,
        )
        return destination

    def ensure_data_file(self) -> Path:
        """确保默认数据文件已经打开，供没有显式 Set Datafile 的 SEQ 使用。"""

        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        if self._data_writer is None:
            self._open_data_file(self.paths.data_file, "open|create")
        return self.paths.data_file

    def _open_data_file(
        self,
        path: Path,
        mode: str,
        *,
        columns: list[str] | None = None,
        append: bool | None = None,
    ) -> None:
        """创建新 DAT 头或在验证后的兼容文件末尾追加。"""

        normalized = self._normalize_datafile_mode(mode)
        planned_columns = columns if columns is not None else self._build_columns()
        should_append = (
            append
            if append is not None
            else self._validate_data_file_plan(path, normalized, planned_columns)
        )
        handle_mode = "a" if should_append else "w"
        self._data_handle = path.open(handle_mode, encoding="utf-8", newline="")
        self._data_writer = csv.writer(self._data_handle, lineterminator="\n")
        self._columns = list(planned_columns)
        self._data_file_initialized = True
        if not should_append:
            self._data_handle.write("[Header]\n")
            self._data_handle.write("; OpenLab Control Data File (default extension .dat)\n")
            self._data_handle.write(
                "; Timestamp(s) uses "
                f"{self.config.logging.timestamp_epoch}.\n"
            )
            self._data_writer.writerow(["BYAPP", "OpenLab Control", __version__])
            self._data_writer.writerow(
                [
                    "TIMESTAMP_EPOCH",
                    self.config.logging.timestamp_epoch,
                ]
            )
            self._data_writer.writerow(["INFO", "System Instrument and Measurement Module framework"])
            self._data_writer.writerow(["INFO", f"Started: {datetime.now().astimezone().isoformat()}"])
            for instrument in self.config.instrument_instances:
                self._data_writer.writerow([
                    "INFO",
                    f"Instrument {instrument.id}: {instrument.display_name}; "
                    f"kind={instrument.kind.value}; "
                    f"control={str(instrument.control_enabled).lower()}; "
                    f"backend={instrument.backend}",
                ])
            for module in self._module_descriptors:
                self._data_writer.writerow([
                    "INFO",
                    f"Module {module.id}: {module.name}; version={module.version}",
                ])
            self._data_handle.write("\n[Data]\n")
            self._data_writer.writerow(self._columns)
        elif path.stat().st_size > 0 and not self._ends_with_newline(path):
            self._data_handle.write("\n")
        self._flush_data()

    @staticmethod
    def _normalize_datafile_mode(mode: str) -> str:
        """规范化并限制为 open、create 或 open|create。"""

        normalized = mode.strip().casefold()
        if normalized not in DATAFILE_MODES:
            allowed = ", ".join(sorted(DATAFILE_MODES))
            raise ValueError(f"Unknown data file mode {mode!r}; expected one of: {allowed}")
        return normalized

    def _validate_data_file_plan(
        self,
        path: Path,
        mode: str,
        columns: list[str],
    ) -> bool:
        """在打开文件前验证存在性和列结构；返回是否应追加。"""

        if mode == "open" and not path.exists():
            raise FileNotFoundError(path)
        append = (
            mode in {"open", "open|create"}
            and path.exists()
            and path.stat().st_size > 0
        )
        if append:
            existing = self._read_data_columns(path)
            if existing != columns:
                raise ValueError(
                    "Cannot append to a DAT file with a different schema: "
                    f"expected {columns!r}, found {existing!r}"
                )
        return append

    @staticmethod
    def _read_data_columns(path: Path) -> list[str]:
        """只读取已有 UTF-8 DAT 的 [Data] 列头用于追加兼容性检查。"""

        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for line in handle:
                    if line.strip().casefold() == "[data]":
                        break
                else:
                    raise ValueError(
                        f"Cannot append to {path}: the file has no [Data] section"
                    )
                for record in csv.reader(handle):
                    values = [cell.strip() for cell in record]
                    if not any(values):
                        continue
                    if values[0].lstrip().startswith(";"):
                        continue
                    return values
        except UnicodeError as exc:
            raise ValueError(
                f"Cannot append to {path}: the file is not UTF-8"
            ) from exc
        raise ValueError(f"Cannot append to {path}: the file has no column header")

    @staticmethod
    def _ends_with_newline(path: Path) -> bool:
        """检查非空文件末尾是否已有换行，避免追加首行粘连。"""

        with path.open("rb") as handle:
            handle.seek(-1, 2)
            return handle.read(1) in {b"\n", b"\r"}

    def _build_columns(self) -> list[str]:
        """按仪表配置和模块清单确定本次运行固定的 DAT 列顺序。"""

        columns = ["Timestamp(s)", "Time(s)", "SequenceStep"]
        temperature_count = sum(
            item.kind is InstrumentKind.TEMPERATURE
            for item in self.config.instrument_instances
        )
        field_count = sum(
            item.kind is InstrumentKind.FIELD
            for item in self.config.instrument_instances
        )
        for instrument in self.config.instrument_instances:
            if instrument.kind is InstrumentKind.TEMPERATURE:
                prefix = "Temp" if temperature_count == 1 else f"{instrument.id}.Temp"
                columns.extend([f"{prefix}({instrument.unit})", f"{prefix}Target({instrument.unit})"])
            elif instrument.kind is InstrumentKind.FIELD:
                prefix = "Field" if field_count == 1 else f"{instrument.id}.Field"
                columns.extend([f"{prefix}({instrument.unit})", f"{prefix}Target({instrument.unit})"])
            elif instrument.kind is InstrumentKind.MONITOR:
                columns.append(f"{instrument.id}({instrument.unit})" if instrument.unit else instrument.id)
            columns.extend(
                self._metric_columns(instrument.id)
            )
        for module in self._module_descriptors:
            columns.extend(f"{module.id}.{column.label}" for column in module.columns)
        return columns

    def _open_instrument_status_file(self, path: Path) -> None:
        """创建每次 Run 独立的仪表状态宽表并写入固定列头。"""

        self._instrument_status_handle = path.open(
            "w",
            encoding="utf-8",
            newline="",
        )
        self._instrument_status_handle.write("[Header]\n")
        self._instrument_status_handle.write(
            "; OpenLab Control Instrument Status Log\n"
        )
        self._instrument_status_handle.write(
            "; One row records all configured instruments; "
            "the default interval is controlled by "
            "logging.instrument_status_interval_seconds.\n"
        )
        self._instrument_status_writer = csv.writer(
            self._instrument_status_handle,
            lineterminator="\n",
        )
        self._instrument_status_writer.writerow(
            ["BYAPP", "OpenLab Control", __version__]
        )
        self._instrument_status_writer.writerow(
            [
                "TIMESTAMP_EPOCH",
                self.config.logging.timestamp_epoch,
            ]
        )
        self._instrument_status_writer.writerow(
            [
                "INFO",
                (
                    "Started: "
                    f"{datetime.now().astimezone().isoformat()}"
                ),
            ]
        )
        for instrument in self.config.instrument_instances:
            self._instrument_status_writer.writerow(
                [
                    "INFO",
                    (
                        f"Instrument {instrument.id}: {instrument.display_name}; "
                        f"kind={instrument.kind.value}; "
                        f"control={str(instrument.control_enabled).lower()}; "
                        f"unit={instrument.unit}"
                    ),
                ]
            )
        self._instrument_status_handle.write("\n[Data]\n")
        self._instrument_status_writer.writerow(
            self._instrument_status_columns()
        )
        self._instrument_status_handle.flush()

    def _instrument_status_columns(self) -> list[str]:
        """按配置顺序构造稳定且可直接导入表格软件的状态列。"""

        columns = ["Timestamp(s)", "Time(s)"]
        for instrument in self.config.instrument_instances:
            controller_panels = tuple(
                panel
                for panel in instrument.panels
                if panel.enabled and panel.template == "controller"
            )
            for panel in controller_panels:
                prefix = panel.key
                unit = instrument.reading(panel.reading).unit
                unit_suffix = f"({unit})" if unit else ""
                rate_suffix = f"({unit}/min)" if unit else ""
                columns.extend(
                    [
                        f"{prefix}.Current{unit_suffix}",
                        f"{prefix}.Target{unit_suffix}",
                        f"{prefix}.Rate{rate_suffix}",
                        f"{prefix}.Activity",
                        f"{prefix}.Stability",
                        f"{prefix}.Ready",
                    ]
                )
            if (
                not controller_panels
                and instrument.kind is InstrumentKind.MONITOR
            ):
                unit_suffix = f"({instrument.unit})" if instrument.unit else ""
                columns.append(f"{instrument.id}.Current{unit_suffix}")
            columns.extend(
                [
                    f"{instrument.id}.Connection",
                    f"{instrument.id}.ReadingAge(s)",
                    f"{instrument.id}.Message",
                ]
            )
            columns.extend(self._metric_columns(instrument.id))
        return columns

    def _metric_columns(self, instrument_id: str) -> list[str]:
        """按 Run 开始时冻结的附加读数 Schema 构造列名。"""

        return [
            (
                f"{instrument_id}.{metric_key}({metric.unit})"
                if metric.unit
                else f"{instrument_id}.{metric_key}"
            )
            for metric_key, metric in self._instrument_metric_schemas.get(
                instrument_id,
                (),
            )
        ]

    def _metric_values(
        self,
        instrument_id: str,
        snapshot: InstrumentSnapshot | None,
    ) -> list[str]:
        """按冻结列顺序提取附加读数；缺失值留空而不沿用旧样本。"""

        actual = {
            metric_key: metric.value
            for metric_key, metric in (
                () if snapshot is None else snapshot.metrics.items()
            )
        }
        return [
            self._format_metric_value(actual.get(metric_key), metric.decimals)
            for metric_key, metric in self._instrument_metric_schemas.get(
                instrument_id,
                (),
            )
        ]

    @staticmethod
    def _format_metric_value(value: Any, decimals: int | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)):
            return (
                fixed_number(float(value), decimals)
                if decimals is not None
                else f"{value:.9g}"
            )
        return str(value)

    def write_instrument_status(
        self,
        snapshots: Mapping[str, InstrumentSnapshot],
        *,
        force: bool = False,
    ) -> bool:
        """在 Run 活动时按独立周期记录全部仪表状态。

        返回值说明本次是否真正写入。Run 尚未开始或刚写过一行时安静返回
        ``False``，因此后台轮询可以无条件调用而不创建空闲期文件。
        """

        if self._instrument_status_writer is None:
            return False
        now = time.monotonic()
        previous = self._last_instrument_status_monotonic
        if (
            not force
            and previous is not None
            and (
                now - previous
                < self.config.logging.instrument_status_interval_seconds
            )
        ):
            return False
        unix_now = time.time()
        absolute = (
            unix_now + LABVIEW_UNIX_OFFSET_SECONDS
            if self.config.logging.timestamp_epoch == "labview_1904"
            else unix_now
        )
        row: list[object] = [
            f"{absolute:.2f}",
            f"{now - self._started_monotonic:.2f}",
        ]
        for instrument in self.config.instrument_instances:
            controller_panels = tuple(
                panel
                for panel in instrument.panels
                if panel.enabled and panel.template == "controller"
            )
            snapshot = snapshots.get(instrument.id)
            if snapshot is None:
                row.extend("" for _ in range(6 * len(controller_panels)))
                if (
                    not controller_panels
                    and instrument.kind is InstrumentKind.MONITOR
                ):
                    row.append("")
                row.extend(["", "", "No snapshot"])
                row.extend(self._metric_values(instrument.id, None))
                continue
            for panel in controller_panels:
                state = snapshot.controls[panel.id]
                reading = instrument.reading(panel.reading)
                decimals = (
                    reading.decimals
                    if reading.decimals is not None
                    else control_decimals(instrument.kind, reading.unit)
                )
                row.extend(
                    [
                        (
                            ""
                            if state.current is None
                            else fixed_number(state.current, decimals)
                        ),
                        (
                            ""
                            if state.target is None
                            else fixed_number(state.target, decimals)
                        ),
                        (
                            ""
                            if state.rate_per_minute is None
                            else fixed_number(state.rate_per_minute, decimals)
                        ),
                        state.activity.value,
                        state.stability.value,
                        "" if state.ready is None else str(state.ready).lower(),
                    ]
                )
            if (
                not controller_panels
                and instrument.kind is InstrumentKind.MONITOR
            ):
                reading = instrument.reading(instrument.main_reading)
                decimals = reading.decimals if reading.decimals is not None else 3
                row.append(
                    ""
                    if snapshot.current is None
                    else fixed_number(snapshot.current, decimals)
                )
            row.extend(
                [
                    snapshot.connection_state.value,
                    f"{max(0.0, now - snapshot.timestamp):.3f}",
                    snapshot.message,
                ]
            )
            row.extend(self._metric_values(instrument.id, snapshot))
        self._instrument_status_writer.writerow(row)
        self._last_instrument_status_monotonic = now
        if (
            self.config.logging.flush_every_row
            and self._instrument_status_handle is not None
        ):
            self._instrument_status_handle.flush()
        return True

    def write_module_row(
        self,
        snapshots: dict[str, InstrumentSnapshot],
        module_id: str,
        values: Mapping[str, Any],
        sequence_step: str,
        *,
        raw_values: tuple[float, ...] | None = None,
    ) -> None:
        """写入一个模块结果行及其可选原始序列。

        rawdata 文件无表头、无时间戳、无通道名，每行仅包含该正式 DAT 行对应的原始
        数值。文件按“当前 DAT 文件 + 模块”拆分，SEQ 中切换 Datafile 时不会把不同
        正式数据文件的原始行混在一起。
        """

        self.write_measurement_row(
            snapshots,
            {module_id: values},
            sequence_step,
            raw_values=(
                {}
                if raw_values is None
                else {module_id: raw_values}
            ),
        )

    def write_measurement_row(
        self,
        snapshots: dict[str, InstrumentSnapshot],
        module_values: Mapping[str, Mapping[str, Any]],
        sequence_step: str,
        *,
        raw_values: Mapping[str, tuple[float, ...]] | None = None,
    ) -> None:
        """把同一逻辑槽位内多个模块的结果合并为一个正式 DAT 行。

        未出现在 ``module_values`` 的模块保持空列。每个模块的 rawdata 仍写入自己的
        sidecar；同一正式行可以同时对应多个模块各自的一条 rawdata 行。
        """

        self.ensure_data_file()
        assert self._data_writer is not None
        self._data_writer.writerow(
            self._row(
                snapshots,
                module_values,
                sequence_step,
            )
        )
        for module_id, values in (raw_values or {}).items():
            self._write_raw_row(module_id, values)
        self._flush_data()

    def _write_raw_row(
        self,
        module_id: str,
        values: tuple[float, ...],
    ) -> None:
        """把已验证的原始值写入与当前 DAT 对应的模块 sidecar。"""

        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        data_path = self.paths.data_file.resolve()
        key = (data_path, module_id)
        writer = self._raw_writers.get(key)
        if writer is None:
            self.paths.raw_data_directory.mkdir(
                parents=True,
                exist_ok=True,
            )
            path = self._raw_sidecar_path(
                data_path,
                module_id,
            )
            handle = path.open("a", encoding="utf-8", newline="")
            writer = csv.writer(handle, lineterminator="\n")
            self._raw_handles[key] = handle
            self._raw_writers[key] = writer
        writer.writerow(f"{value:.17g}" for value in values)
        if self.config.logging.flush_every_row:
            self._raw_handles[key].flush()

    def _raw_sidecar_path(
        self,
        data_path: Path,
        module_id: str,
    ) -> Path:
        """为一个正式 DAT 生成稳定且不泄露目录名的 rawdata 文件名。

        不同目录可以存在同名 ``sample.dat``。短路径摘要用于消除这种冲突；文件名仍
        保留 DAT stem 和模块 ID，便于人工识别。
        """

        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        resolved = data_path.resolve()
        digest = hashlib.sha256(
            str(resolved).encode("utf-8")
        ).hexdigest()[:10]
        data_stem = self._safe_name(resolved.stem)
        safe_module_id = self._safe_name(module_id)
        return (
            self.paths.raw_data_directory
            / f"{data_stem}__{digest}__{safe_module_id}.rawdata"
        )

    def _close_raw_datafile(self, data_path: Path) -> None:
        """关闭属于一个正式 DAT 的 sidecar，切换回来时再以追加模式打开。"""

        resolved = data_path.resolve()
        keys = [
            key
            for key in self._raw_handles
            if key[0] == resolved
        ]
        first_error: Exception | None = None
        for key in keys:
            handle = self._raw_handles.pop(key)
            self._raw_writers.pop(key, None)
            failure = self._flush_and_close_handle(handle)
            first_error = first_error or failure
        if first_error is not None:
            raise first_error

    def _remove_raw_sidecars(
        self,
        data_path: Path,
    ) -> None:
        """删除即将被 ``create`` 重建的 DAT 所对应的本 Run 原始数据。"""

        if self.paths is None:
            raise RuntimeError("Run directory has not been created")
        resolved = data_path.resolve()
        self._close_raw_datafile(resolved)
        for descriptor in self._module_descriptors:
            sidecar = self._raw_sidecar_path(
                resolved,
                descriptor.id,
            )
            if sidecar.exists():
                sidecar.unlink()

    def write_system_row(
        self,
        snapshots: dict[str, InstrumentSnapshot],
        sequence_step: str,
    ) -> None:
        """写入只含系统仪表快照、不含模块结果的行。"""

        self.ensure_data_file()
        assert self._data_writer is not None
        self._data_writer.writerow(
            self._row(snapshots, {}, sequence_step)
        )
        self._flush_data()

    def _row(
        self,
        snapshots: dict[str, InstrumentSnapshot],
        module_values: Mapping[str, Mapping[str, Any]],
        sequence_step: str,
    ) -> list[object]:
        """按固定列结构组装单行，并使用规定精度处理仪表和模块数值。"""

        unix_now = time.time()
        absolute = (
            unix_now + LABVIEW_UNIX_OFFSET_SECONDS
            if self.config.logging.timestamp_epoch == "labview_1904"
            else unix_now
        )
        row: list[object] = [
            f"{absolute:.2f}",
            f"{time.monotonic() - self._started_monotonic:.2f}",
            sequence_step,
        ]
        for instrument in self.config.instrument_instances:
            snapshot = snapshots.get(instrument.id)
            usable_snapshot = (
                snapshot
                if snapshot is not None and snapshot.connected
                else None
            )
            if instrument.kind in (InstrumentKind.TEMPERATURE, InstrumentKind.FIELD):
                decimals = control_decimals(instrument.kind, instrument.unit)
                row.extend(
                    [
                        (
                            ""
                            if usable_snapshot is None
                            or usable_snapshot.current is None
                            else fixed_number(
                                usable_snapshot.current,
                                decimals,
                            )
                        ),
                        (
                            ""
                            if usable_snapshot is None
                            or usable_snapshot.target is None
                            else fixed_number(
                                usable_snapshot.target,
                                decimals,
                            )
                        ),
                    ]
                )
            elif instrument.kind is InstrumentKind.MONITOR:
                row.append(
                    ""
                    if usable_snapshot is None or usable_snapshot.current is None
                    else fixed_number(usable_snapshot.current, 3)
                )
            row.extend(self._metric_values(instrument.id, usable_snapshot))
        for module in self._module_descriptors:
            values = module_values.get(module.id, {})
            for column in module.columns:
                value = values.get(column.name)
                if value is None:
                    row.append("")
                elif isinstance(value, float):
                    row.append(f"{value:.9g}")
                else:
                    row.append(str(value))
        return row

    def _open_event_file(self, path: Path) -> None:
        """创建本次运行的独立事件日志并立即写入列头。"""

        self._event_handle = path.open("w", encoding="utf-8", newline="")
        self._event_handle.write("[Header]\n")
        self._event_handle.write("; OpenLab Control Event Log\n\n[Events]\n")
        self._event_writer = csv.writer(self._event_handle, lineterminator="\n")
        self._event_writer.writerow([
            "Timestamp(s)", "ISO8601", "Severity", "Source", "Code", "State", "Count", "Context", "Message"
        ])
        self._event_handle.flush()

    def on_event(self, notice: EventNotice) -> None:
        """接收事件；运行目录建立前先排队，之后立即落盘。"""

        if self._event_writer is None:
            self._pending_events.append(notice)
        else:
            self._write_event(notice)

    def _write_event(self, notice: EventNotice) -> None:
        """写入 RAISED/RESOLVED 事件及其锁存计数。"""

        if self._event_writer is None:
            return
        event = notice.event
        timestamp = (
            event.resolved_at
            if notice.is_resolution and event.resolved_at is not None
            else event.timestamp
        )
        unix = timestamp.timestamp()
        absolute = unix + LABVIEW_UNIX_OFFSET_SECONDS if self.config.logging.timestamp_epoch == "labview_1904" else unix
        self._event_writer.writerow([
            f"{absolute:.2f}",
            timestamp.isoformat(),
            event.severity.value,
            event.source,
            event.code,
            "RESOLVED" if notice.is_resolution else "RAISED",
            event.count,
            event.context,
            event.message,
        ])
        if self._event_handle is not None:
            self._event_handle.flush()

    def _flush_data(self) -> None:
        """按配置决定是否每行刷新，降低异常退出时的数据损失。"""

        if self.config.logging.flush_every_row and self._data_handle is not None:
            self._data_handle.flush()

    def _close_data_file(self) -> None:
        """刷新并关闭当前 DAT；可在切换 Set Datafile 时重复调用。"""

        handle = self._data_handle
        self._data_handle = None
        self._data_writer = None
        if handle is not None:
            failure = self._flush_and_close_handle(handle)
            if failure is not None:
                raise failure

    def close(self) -> None:
        """确保至少生成默认 DAT，并在单个 flush 失败时仍释放其余全部句柄。"""

        first_error: Exception | None = None
        try:
            if (
                self.paths is not None
                and not self._data_file_initialized
            ):
                self.ensure_data_file()
            self._close_data_file()
        except Exception as exc:
            first_error = exc
        for handle in tuple(self._raw_handles.values()):
            failure = self._flush_and_close_handle(handle)
            first_error = first_error or failure
        self._raw_handles.clear()
        self._raw_writers.clear()
        if self._instrument_status_handle is not None:
            failure = self._flush_and_close_handle(
                self._instrument_status_handle
            )
            first_error = first_error or failure
        self._instrument_status_handle = None
        self._instrument_status_writer = None
        self._last_instrument_status_monotonic = None
        if self._event_handle is not None:
            failure = self._flush_and_close_handle(
                self._event_handle
            )
            first_error = first_error or failure
        self._event_handle = None
        self._event_writer = None
        if first_error is not None:
            raise first_error

    @staticmethod
    def _flush_and_close_handle(
        handle: TextIO,
    ) -> Exception | None:
        """返回首个 I/O 错误，但无论 flush 是否成功都继续尝试 close。"""

        first_error: Exception | None = None
        try:
            handle.flush()
        except Exception as exc:
            first_error = exc
        try:
            handle.close()
        except Exception as exc:
            first_error = first_error or exc
        return first_error

    @staticmethod
    def _safe_name(value: str) -> str:
        """把 SEQ 名称缩减为可安全用于 Windows 目录的 ASCII 片段。"""

        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        cleaned = "".join(character if character in allowed else "_" for character in value)
        return cleaned.strip("_") or "sequence"
