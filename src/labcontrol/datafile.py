"""一次 SEQ 运行的目录、快照、DAT 数据、设备状态和事件日志写入。

运行开始即复制规范化 SEQ、主配置、各模块期望设置和启动状态。DAT 列由全部设备与已启用
模块清单一次构造；一个模块返回多行时每行独立写入，只有该模块的列填值，其他模块列留空。
设备状态按独立节流周期写入固定宽表，不受 Measure 数量影响。追加已有 DAT 前必须验证完整
列结构一致，防止动态列变化造成静默错位。
"""

from __future__ import annotations

import csv
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
from .models import DeviceKind, DeviceSnapshot, EventNotice, Severity
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
    device_status_file: Path
    sequence_snapshot: Path
    configuration_snapshot: Path
    module_settings_directory: Path


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
        self._device_status_handle: TextIO | None = None
        self._device_status_writer: csv.writer | None = None
        self._last_device_status_monotonic: float | None = None
        self._columns: list[str] = []
        self._data_file_initialized = False
        self._module_descriptors: tuple[ModuleDescriptor, ...] = ()
        self._pending_events: list[EventNotice] = []
        self.events.subscribe(self.on_event)

    def open_run(
        self,
        sequence_name: str,
        sequence_text: str,
        module_descriptors: tuple[ModuleDescriptor, ...] = (),
        module_settings: Mapping[str, Mapping[str, Any]] | None = None,
        module_status: Mapping[str, Mapping[str, Any]] | None = None,
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
        device_status_file = (
            directory
            / self.config.logging.device_status_file_name
        )
        sequence_snapshot = directory / "sequence.seq"
        config_snapshot = directory / "configuration.toml"
        module_settings_directory = directory / "module_settings"
        module_settings_directory.mkdir()
        sequence_snapshot.write_text(sequence_text, encoding="utf-8", newline="\n")
        shutil.copy2(self.config.source_path, config_snapshot)
        self._module_descriptors = tuple(module_descriptors)
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
            device_status_file=device_status_file,
            sequence_snapshot=sequence_snapshot,
            configuration_snapshot=config_snapshot,
            module_settings_directory=module_settings_directory,
        )
        self._data_file_initialized = False
        self._started_monotonic = time.monotonic()
        self._last_device_status_monotonic = None
        self._open_event_file(event_file)
        self._open_device_status_file(device_status_file)
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
        reserved_paths = {
            self.paths.directory.resolve(),
            self.paths.event_file.resolve(),
            self.paths.device_status_file.resolve(),
            self.paths.sequence_snapshot.resolve(),
            self.paths.configuration_snapshot.resolve(),
            module_settings_resolved,
        }
        if (
            destination_resolved in reserved_paths
            or module_settings_resolved
            in destination_resolved.parents
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
            device_status_file=self.paths.device_status_file,
            sequence_snapshot=self.paths.sequence_snapshot,
            configuration_snapshot=self.paths.configuration_snapshot,
            module_settings_directory=self.paths.module_settings_directory,
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
            self._data_writer.writerow(["INFO", "Plugin-oriented laboratory control framework"])
            self._data_writer.writerow(["INFO", f"Started: {datetime.now().astimezone().isoformat()}"])
            for device in self.config.devices:
                self._data_writer.writerow([
                    "INFO",
                    f"Device {device.id}: {device.display_name}; "
                    f"kind={device.kind.value}; role={device.role.value}; "
                    f"control={str(device.control_enabled).lower()}; "
                    f"plugin={device.plugin}",
                ])
            for module in self._module_descriptors:
                self._data_writer.writerow([
                    "INFO",
                    f"Module {module.id}: {module.name}; version={module.version}; api={module.api_version}",
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
        """按设备配置和模块清单确定本次运行固定的 DAT 列顺序。"""

        columns = ["Timestamp(s)", "Time(s)", "SequenceStep"]
        temperature_count = sum(
            item.kind is DeviceKind.TEMPERATURE for item in self.config.devices
        )
        field_count = sum(item.kind is DeviceKind.FIELD for item in self.config.devices)
        for device in self.config.devices:
            if device.kind is DeviceKind.TEMPERATURE:
                prefix = "Temp" if temperature_count == 1 else f"{device.id}.Temp"
                columns.extend([f"{prefix}({device.unit})", f"{prefix}Target({device.unit})"])
            elif device.kind is DeviceKind.FIELD:
                prefix = "Field" if field_count == 1 else f"{device.id}.Field"
                columns.extend([f"{prefix}({device.unit})", f"{prefix}Target({device.unit})"])
            elif device.kind is DeviceKind.MONITOR:
                columns.append(f"{device.id}({device.unit})" if device.unit else device.id)
        for module in self._module_descriptors:
            columns.extend(f"{module.id}.{column.label}" for column in module.columns)
        return columns

    def _open_device_status_file(self, path: Path) -> None:
        """创建每次 Run 独立的设备状态宽表并写入固定列头。"""

        self._device_status_handle = path.open(
            "w",
            encoding="utf-8",
            newline="",
        )
        self._device_status_handle.write("[Header]\n")
        self._device_status_handle.write(
            "; OpenLab Control Device Status Log\n"
        )
        self._device_status_handle.write(
            "; One row records all configured devices; "
            "the default interval is controlled by "
            "logging.device_status_interval_seconds.\n"
        )
        self._device_status_writer = csv.writer(
            self._device_status_handle,
            lineterminator="\n",
        )
        self._device_status_writer.writerow(
            ["BYAPP", "OpenLab Control", __version__]
        )
        self._device_status_writer.writerow(
            [
                "TIMESTAMP_EPOCH",
                self.config.logging.timestamp_epoch,
            ]
        )
        self._device_status_writer.writerow(
            [
                "INFO",
                (
                    "Started: "
                    f"{datetime.now().astimezone().isoformat()}"
                ),
            ]
        )
        for device in self.config.devices:
            self._device_status_writer.writerow(
                [
                    "INFO",
                    (
                        f"Device {device.id}: {device.display_name}; "
                        f"kind={device.kind.value}; "
                        f"role={device.role.value}; "
                        f"unit={device.unit}"
                    ),
                ]
            )
        self._device_status_handle.write("\n[Data]\n")
        self._device_status_writer.writerow(
            self._device_status_columns()
        )
        self._device_status_handle.flush()

    def _device_status_columns(self) -> list[str]:
        """按配置顺序构造稳定且可直接导入表格软件的状态列。"""

        columns = ["Timestamp(s)", "Time(s)"]
        for device in self.config.devices:
            prefix = device.id
            unit_suffix = (
                f"({device.unit})"
                if device.unit
                else ""
            )
            rate_suffix = (
                f"({device.unit}/min)"
                if device.unit
                else ""
            )
            columns.extend(
                [
                    f"{prefix}.Current{unit_suffix}",
                    f"{prefix}.Target{unit_suffix}",
                    f"{prefix}.Rate{rate_suffix}",
                    f"{prefix}.Activity",
                    f"{prefix}.Stability",
                    f"{prefix}.Connection",
                    f"{prefix}.Connected",
                    f"{prefix}.ReadingAge(s)",
                    f"{prefix}.Message",
                ]
            )
        return columns

    def write_device_status(
        self,
        snapshots: Mapping[str, DeviceSnapshot],
        *,
        force: bool = False,
    ) -> bool:
        """在 Run 活动时按独立周期记录全部设备状态。

        返回值说明本次是否真正写入。Run 尚未开始或刚写过一行时安静返回
        ``False``，因此后台轮询可以无条件调用而不创建空闲期文件。
        """

        if self._device_status_writer is None:
            return False
        now = time.monotonic()
        previous = self._last_device_status_monotonic
        if (
            not force
            and previous is not None
            and (
                now - previous
                < self.config.logging.device_status_interval_seconds
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
        for device in self.config.devices:
            snapshot = snapshots.get(device.id)
            if snapshot is None:
                row.extend(
                    [
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "false",
                        "",
                        "No snapshot",
                    ]
                )
                continue
            decimals = (
                control_decimals(device.kind, device.unit)
                if device.kind
                in (DeviceKind.TEMPERATURE, DeviceKind.FIELD)
                else 3
            )
            row.extend(
                [
                    (
                        ""
                        if snapshot.current is None
                        else fixed_number(
                            snapshot.current,
                            decimals,
                        )
                    ),
                    (
                        ""
                        if snapshot.target is None
                        else fixed_number(
                            snapshot.target,
                            decimals,
                        )
                    ),
                    (
                        ""
                        if snapshot.rate_per_minute is None
                        else fixed_number(
                            snapshot.rate_per_minute,
                            decimals,
                        )
                    ),
                    snapshot.activity.value,
                    snapshot.stability.value,
                    snapshot.connection_state.value,
                    str(snapshot.connected).lower(),
                    f"{max(0.0, now - snapshot.timestamp):.3f}",
                    snapshot.message,
                ]
            )
        self._device_status_writer.writerow(row)
        self._last_device_status_monotonic = now
        if (
            self.config.logging.flush_every_row
            and self._device_status_handle is not None
        ):
            self._device_status_handle.flush()
        return True

    def write_module_row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        module_id: str,
        values: Mapping[str, Any],
        sequence_step: str,
    ) -> None:
        """写入一个模块结果行；其他模块的动态列保持为空。"""

        self.ensure_data_file()
        assert self._data_writer is not None
        self._data_writer.writerow(self._row(snapshots, module_id, values, sequence_step))
        self._flush_data()

    def write_system_row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        sequence_step: str,
    ) -> None:
        """写入只含系统设备快照、不含模块结果的行。"""

        self.ensure_data_file()
        assert self._data_writer is not None
        self._data_writer.writerow(self._row(snapshots, None, {}, sequence_step))
        self._flush_data()

    def _row(
        self,
        snapshots: dict[str, DeviceSnapshot],
        module_id: str | None,
        values: Mapping[str, Any],
        sequence_step: str,
    ) -> list[object]:
        """按固定列结构组装单行，并使用规定精度处理设备和模块数值。"""

        unix_now = time.time()
        absolute = (
            unix_now + LABVIEW_UNIX_OFFSET_SECONDS
            if self.config.logging.timestamp_epoch == "labview_1904"
            else unix_now
        )
        row: list[object] = [f"{absolute:.2f}", f"{time.monotonic() - self._started_monotonic:.2f}", sequence_step]
        for device in self.config.devices:
            snapshot = snapshots.get(device.id)
            if device.kind in (DeviceKind.TEMPERATURE, DeviceKind.FIELD):
                decimals = control_decimals(device.kind, device.unit)
                row.extend([
                    "" if snapshot is None or snapshot.current is None else fixed_number(snapshot.current, decimals),
                    "" if snapshot is None or snapshot.target is None else fixed_number(snapshot.target, decimals),
                ])
            elif device.kind is DeviceKind.MONITOR:
                row.append(
                    ""
                    if snapshot is None or snapshot.current is None
                    else fixed_number(snapshot.current, 3)
                )
        for module in self._module_descriptors:
            for column in module.columns:
                value = values.get(column.name) if module.id == module_id else None
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

        if self._data_handle is not None:
            self._data_handle.flush()
            self._data_handle.close()
        self._data_handle = None
        self._data_writer = None

    def close(self) -> None:
        """确保至少生成默认 DAT，然后关闭数据、状态和事件句柄。"""

        if self.paths is not None and not self._data_file_initialized:
            self.ensure_data_file()
        self._close_data_file()
        if self._device_status_handle is not None:
            self._device_status_handle.flush()
            self._device_status_handle.close()
        self._device_status_handle = None
        self._device_status_writer = None
        self._last_device_status_monotonic = None
        if self._event_handle is not None:
            self._event_handle.flush()
            self._event_handle.close()
        self._event_handle = None
        self._event_writer = None

    @staticmethod
    def _safe_name(value: str) -> str:
        """把 SEQ 名称缩减为可安全用于 Windows 目录的 ASCII 片段。"""

        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        cleaned = "".join(character if character in allowed else "_" for character in value)
        return cleaned.strip("_") or "sequence"
