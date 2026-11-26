"""SEQ 编辑器和执行引擎共享的树形文档模型。

扫描命令通过 ``children`` 任意嵌套；文件中的 ``T/F`` 只保存在每个命令自身，实际是否执行
还要与所有父节点状态合并。编辑器使用随机稳定 ID 定位节点，复制命令时必须为整棵子树刷新
ID，避免视图操作命中原节点。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..formatting import field_decimals, require_precision


class CommandType(str, Enum):
    """框架支持的规范化 SEQ 命令类型。"""

    SET_DATAFILE = "set_datafile"
    WAIT = "wait"
    SET_TEMPERATURE = "set_temperature"
    SET_FIELD = "set_field"
    SCAN_TEMPERATURE = "scan_temperature"
    SCAN_FIELD = "scan_field"
    SCAN_TIME = "scan_time"
    MEASURE = "measure"
    REMARK = "remark"
    CALL_SEQUENCE = "call_sequence"
    INJECT_WARNING = "inject_warning"
    INJECT_ERROR = "inject_error"
    INSTRUMENT_COMMAND = "instrument_command"
    MODULE_COMMAND = "module_command"
    MODULE_SCAN = "module_scan"
    UNKNOWN = "unknown"

    @property
    def is_container(self) -> bool:
        """该命令是否拥有由 ``End Scan`` 闭合的子命令列表。"""

        return self in {
            CommandType.SCAN_TEMPERATURE,
            CommandType.SCAN_FIELD,
            CommandType.SCAN_TIME,
            CommandType.MODULE_SCAN,
        }


@dataclass(slots=True)
class Command:
    """一个 SEQ 命令节点及其参数、子节点和源文件信息。"""

    type: CommandType
    params: dict[str, Any] = field(default_factory=dict)
    children: list["Command"] = field(default_factory=list)
    raw_text: str | None = None
    source_line: int | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    enabled: bool = True
    # 模块 ID 与其稳定指令 ID 是通用 SEQ 信封的一部分，不混入作者自己的参数名，
    # 因而模块可以自由使用 ``module_id`` 等普通字段而不会与核心保留键冲突。
    module_id: str = ""
    module_command_id: str = ""
    # System Instrument 指令同样把核心信封与作者的指令 ID 分开；当前接口没有参数。
    instrument_id: str = ""
    instrument_command_id: str = ""

    def update_params(self, values: dict[str, Any]) -> None:
        """替换编辑后的参数，并丢弃不再可信的原始文本缓存。"""

        self.params = dict(values)
        self.raw_text = None

    def clone(self) -> "Command":
        """深拷贝整棵子树，并为每个节点生成新的编辑器 ID。"""

        duplicate = deepcopy(self)

        def refresh(command: Command) -> None:
            command.id = uuid4().hex
            for child in command.children:
                refresh(child)

        refresh(duplicate)
        return duplicate


@dataclass(frozen=True, slots=True)
class SystemInstrumentCommandSpec:
    """一条可直接插入当前站点 SEQ 的无参数仪表指令。"""

    instrument_id: str
    command_id: str
    label: str

    def create(self) -> Command:
        return Command(
            CommandType.INSTRUMENT_COMMAND,
            {"label": self.label},
            instrument_id=self.instrument_id,
            instrument_command_id=self.command_id,
        )


@dataclass(frozen=True, slots=True)
class FlatRow:
    """树形命令在表格中的一行，可代表命令、End Scan 或 End Sequence。"""

    command_id: str | None
    depth: int
    is_end: bool
    is_sequence_end: bool = False
    effective_enabled: bool = True


@dataclass(slots=True)
class SequenceDocument:
    """一份可编辑的 SEQ 树及其来源路径。"""

    commands: list[Command] = field(default_factory=list)
    name: str = "Untitled.seq"
    path: Path | None = None

    def clone(self) -> "SequenceDocument":
        """复制文档快照，供跨线程执行时与 UI 后续编辑隔离。"""

        return deepcopy(self)

    def flat_rows(self) -> list[FlatRow]:
        """按深度优先顺序展开表格行，并计算继承父节点后的有效启用状态。"""

        rows: list[FlatRow] = []

        def visit(commands: list[Command], depth: int, parent_enabled: bool) -> None:
            for command in commands:
                effective_enabled = parent_enabled and command.enabled
                rows.append(FlatRow(command.id, depth, False, effective_enabled=effective_enabled))
                if command.type.is_container:
                    visit(command.children, depth + 1, effective_enabled)
                    rows.append(FlatRow(command.id, depth, True, effective_enabled=effective_enabled))

        visit(self.commands, 0, True)
        rows.append(FlatRow(None, 0, False, is_sequence_end=True))
        return rows

    def find(self, command_id: str) -> Command | None:
        """在任意嵌套层级查找命令。"""

        def search(commands: list[Command]) -> Command | None:
            for command in commands:
                if command.id == command_id:
                    return command
                found = search(command.children)
                if found is not None:
                    return found
            return None

        return search(self.commands)

    def _locate(self, command_id: str) -> tuple[list[Command], int] | None:
        """返回节点所在的可变兄弟列表和索引，供编辑操作使用。"""

        def search(commands: list[Command]) -> tuple[list[Command], int] | None:
            for index, command in enumerate(commands):
                if command.id == command_id:
                    return commands, index
                found = search(command.children)
                if found is not None:
                    return found
            return None

        return search(self.commands)

    def insert(self, command: Command, selected: FlatRow | None = None, *, before: bool = False) -> None:
        """按选择插入；before 插在同级行前，End Scan 始终追加到扫描内部。"""

        if selected is None or selected.is_sequence_end:
            self.commands.append(command)
            return
        if selected.command_id is None:
            self.commands.append(command)
            return
        selected_command = self.find(selected.command_id)
        if selected.is_end or (not before and selected_command is not None and selected_command.type.is_container):
            if selected_command is not None:
                selected_command.children.append(command)
                return
        location = self._locate(selected.command_id)
        if location is None:
            self.commands.append(command)
            return
        parent, index = location
        parent.insert(index if before else index + 1, command)

    def insert_after(self, command: Command, anchor_id: str) -> None:
        """在锚点的同级位置之后插入，找不到锚点时追加到根级。"""

        location = self._locate(anchor_id)
        if location is None:
            self.commands.append(command)
            return
        parent, index = location
        parent.insert(index + 1, command)

    def resolve_drop_target(
        self,
        target_row: FlatRow | None,
        *,
        after: bool = False,
    ) -> tuple[list[Command], int, int]:
        """解析行间拖放位置，返回 ``(目标父列表, 插入下标, 目标缩进深度)``。

        规则覆盖嵌套 Scan 的所有行间缝隙：
        - 拖到 ``Scan`` 标题下方（``after=True``）插入为该 Scan 的首条子命令；
        - 拖到 ``End Scan`` 上方（``after=False``）追加到该 Scan 末尾；
        - 拖到 ``End Scan`` 下方（``after=True``）跳出该 Scan，插在其同级之后。
        """

        if (
            target_row is None
            or target_row.is_sequence_end
            or target_row.command_id is None
        ):
            return self.commands, len(self.commands), 0
        target_command = self.find(target_row.command_id)
        location = self._locate(target_row.command_id)
        if target_command is None or location is None:
            return self.commands, len(self.commands), 0
        parent, index = location
        if target_row.is_end:
            if after:
                return parent, index + 1, target_row.depth
            return target_command.children, len(target_command.children), target_row.depth + 1
        if target_command.type.is_container and after:
            return target_command.children, 0, target_row.depth + 1
        return parent, index + 1 if after else index, target_row.depth

    def insert_at(
        self,
        command: Command,
        target_row: FlatRow | None,
        *,
        after: bool = False,
    ) -> None:
        """按行间拖放语义把命令插入到指定位置（支持移入或移出 Scan）。"""

        parent, index, _ = self.resolve_drop_target(target_row, after=after)
        parent.insert(index, command)

    def _top_level_commands_in_order(
        self,
        command_ids: Sequence[str],
    ) -> list[Command]:
        wanted = set(command_ids)
        if not wanted:
            return []
        result: list[Command] = []

        def visit(commands: list[Command], ancestor_selected: bool) -> None:
            for command in commands:
                selected = command.id in wanted
                if selected and not ancestor_selected:
                    result.append(command)
                visit(command.children, ancestor_selected or selected)

        visit(self.commands, False)
        return result

    @staticmethod
    def _collect_subtree_nodes(commands: Sequence[Command]) -> list[Command]:
        nodes: list[Command] = []

        def visit(items: Sequence[Command]) -> None:
            for command in items:
                nodes.append(command)
                if command.children:
                    visit(command.children)

        visit(commands)
        return nodes

    def can_relocate(
        self,
        command_ids: Sequence[str],
        target_row: FlatRow | None,
        *,
        after: bool = False,
    ) -> bool:
        """检查把所选命令（含完整 Scan 子树）拖放到目标位置是否合法（无自嵌套环）。"""

        moved = self._top_level_commands_in_order(command_ids)
        if not moved:
            return False
        target_parent, _, _ = self.resolve_drop_target(target_row, after=after)
        subtree_nodes = self._collect_subtree_nodes(moved)
        return not any(target_parent is node.children for node in subtree_nodes)

    def relocate(
        self,
        command_ids: Sequence[str],
        target_row: FlatRow | None,
        *,
        after: bool = False,
    ) -> bool:
        """原子把一组命令（含完整 Scan 子树）移动到目标行间位置；原地无变化返回 False。"""

        moved = self._top_level_commands_in_order(command_ids)
        if not moved:
            return False
        target_parent, target_index, _ = self.resolve_drop_target(
            target_row,
            after=after,
        )
        subtree_nodes = self._collect_subtree_nodes(moved)
        if any(target_parent is node.children for node in subtree_nodes):
            return False

        moved_ids = {command.id for command in moved}
        shift = sum(
            1
            for index, existing in enumerate(target_parent)
            if index < target_index and existing.id in moved_ids
        )
        final_index = target_index - shift

        if all(any(existing is command for existing in target_parent) for command in moved):
            before_ids = [existing.id for existing in target_parent]
            remaining_ids = [
                existing.id for existing in target_parent if existing.id not in moved_ids
            ]
            after_ids = (
                remaining_ids[:final_index]
                + [command.id for command in moved]
                + remaining_ids[final_index:]
            )
            if after_ids == before_ids:
                return False

        for command in moved:
            location = self._locate(command.id)
            if location is not None:
                parent, index = location
                del parent[index]
        for offset, command in enumerate(moved):
            target_parent.insert(final_index + offset, command)
        return True

    def delete(self, command_id: str) -> bool:
        """删除命令及其完整子树，并返回是否找到节点。"""

        location = self._locate(command_id)
        if location is None:
            return False
        parent, index = location
        del parent[index]
        return True

    def move(self, command_id: str, offset: int) -> bool:
        """只在同一父节点内移动命令，避免无意改变扫描语义。"""

        location = self._locate(command_id)
        if location is None:
            return False
        parent, index = location
        new_index = index + offset
        if new_index < 0 or new_index >= len(parent):
            return False
        parent[index], parent[new_index] = parent[new_index], parent[index]
        return True

    def set_enabled(self, command_id: str, enabled: bool) -> bool:
        """更新命令自身的 T/F 标志；子节点原标志保持不变。"""

        command = self.find(command_id)
        if command is None or command.enabled is enabled:
            return False
        command.enabled = enabled
        return True

    def count_commands(self, *, enabled_only: bool = False) -> int:
        """递归统计命令；可选择仅统计父链也有效启用的节点。"""

        def count(commands: list[Command], parent_enabled: bool) -> int:
            total = 0
            for command in commands:
                effective_enabled = parent_enabled and command.enabled
                if not enabled_only or effective_enabled:
                    total += 1
                if command.type.is_container and (not enabled_only or effective_enabled):
                    total += count(command.children, effective_enabled)
            return total

        return count(self.commands, True)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个命令参数在编辑器中的类型、默认值和输入范围。"""

    name: str
    label: str
    field_type: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    decimals: int | None = None


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """命令类型对应的编辑器字段集合。"""

    command_type: CommandType
    label: str
    category: str
    fields: tuple[FieldSpec, ...]

    def create(self) -> Command:
        """用字段默认值创建一个新命令节点。"""

        return Command(self.command_type, {item.name: item.default for item in self.fields})


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(CommandType.MEASURE, "Measure", "Measurement Commands", ()),
    CommandSpec(CommandType.SET_DATAFILE, "Set Datafile", "System Commands", (
        FieldSpec("mode", "Mode", "choice", "open|create", choices=("open|create", "create", "open")),
        FieldSpec("path_scope", "Location", "choice", "Run folder", choices=("Run folder", "Custom folder")),
        FieldSpec("path", "Data file", "text", "experiment.dat"),
    )),
    CommandSpec(CommandType.CALL_SEQUENCE, "Call Sequence", "System Commands", (
        FieldSpec("path", "SEQ file", "text", "subsequence.seq"),
    )),
    CommandSpec(CommandType.REMARK, "Remark", "System Commands", (
        FieldSpec("text", "Remark", "text", "Remark"),
    )),
    CommandSpec(CommandType.SCAN_FIELD, "Scan Field", "System Commands", (
        FieldSpec("start", "Start", "float", 0.0),
        FieldSpec("stop", "Stop", "float", 10000.0),
        FieldSpec("unit", "Unit", "choice", "Oe", choices=("Oe", "T")),
        FieldSpec("steps", "Points", "int", 11, 1, 100000),
        FieldSpec("rate", "Rate per minute", "float", 5000.0, 0.000001),
        FieldSpec("mode", "Mode", "choice", "Settle", choices=("Settle", "Sweep")),
        FieldSpec(
            "nearest_polarity",
            "Choose nearer start or its negative",
            "bool",
            False,
        ),
    )),
    CommandSpec(CommandType.SCAN_TEMPERATURE, "Scan Temperature", "System Commands", (
        FieldSpec("point_mode", "Point definition", "choice", "Linear", choices=("Linear", "Log", "List")),
        FieldSpec("start", "Start (K)", "float", 300.0),
        FieldSpec("stop", "Stop (K)", "float", 10.0),
        FieldSpec("steps", "Points", "int", 10, 1, 100000),
        FieldSpec(
            "points",
            "Temperature list (K)",
            "text",
            "[300, 100, 20]",
        ),
        FieldSpec("rate", "Rate (K/min)", "float", 5.0, 0.000001),
        FieldSpec("mode", "Mode", "choice", "Settle", choices=("Settle", "Sweep")),
    )),
    CommandSpec(CommandType.SCAN_TIME, "Scan Time", "System Commands", (
        FieldSpec("duration_seconds", "Duration (s)", "float", 60.0, 0.0, 31536000.0, decimals=1),
        FieldSpec("steps", "Points", "int", 60, 1, 1000000),
    )),
    CommandSpec(CommandType.SET_FIELD, "Set Field", "System Commands", (
        FieldSpec("target", "Target", "float", 0.0),
        FieldSpec("unit", "Unit", "choice", "Oe", choices=("Oe", "T")),
        FieldSpec("rate", "Rate per minute", "float", 5000.0, 0.000001),
    )),
    CommandSpec(CommandType.SET_TEMPERATURE, "Set Temperature", "System Commands", (
        FieldSpec("target", "Target (K)", "float", 300.0),
        FieldSpec("rate", "Rate (K/min)", "float", 5.0, 0.000001),
    )),
    CommandSpec(CommandType.WAIT, "Wait", "System Commands", (
        FieldSpec("wait_temperature", "Wait for Temperature", "bool", False),
        FieldSpec("wait_field", "Wait for Field", "bool", False),
        FieldSpec("seconds", "Delay (s)", "float", 10.0, 0.0, 31536000.0, decimals=1),
    )),
    CommandSpec(CommandType.INJECT_WARNING, "Inject Warning", "Advanced Commands (Simulation)", (
        FieldSpec("code", "Code", "text", "SIM_WARNING"),
        FieldSpec("message", "Message", "text", "Simulated Warning"),
    )),
    CommandSpec(CommandType.INJECT_ERROR, "Inject Error", "Advanced Commands (Simulation)", (
        FieldSpec("code", "Code", "text", "SIM_ERROR"),
        FieldSpec("message", "Message", "text", "Simulated Error"),
    )),
)


SPECS_BY_TYPE = {spec.command_type: spec for spec in COMMAND_SPECS}


def command_decimals(command: Command, field_name: str) -> int:
    """内置 SEQ 编辑与执行验证使用同一套输入精度。"""
    spec = SPECS_BY_TYPE.get(command.type)
    if spec is not None:
        field_spec = next(item for item in spec.fields if item.name == field_name)
        if field_spec.decimals is not None:
            return field_spec.decimals
    if command.type in {CommandType.SET_TEMPERATURE, CommandType.SCAN_TEMPERATURE}:
        return 3
    if command.type in {CommandType.SET_FIELD, CommandType.SCAN_FIELD}:
        return field_decimals(command.params.get("unit", "Oe"))
    return 9


def validate_command_parameters(command: Command) -> tuple[str, ...]:
    """执行编辑器、解析器和运行时共用的基础参数校验。"""
    spec = SPECS_BY_TYPE.get(command.type)
    if spec is None:
        return ()

    issues: list[str] = []
    for field_spec in spec.fields:
        if field_spec.name not in command.params:
            continue
        value = command.params[field_spec.name]
        if field_spec.field_type in {"float", "int"}:
            if isinstance(value, bool):
                issues.append(f"{field_spec.label} must be a number")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                issues.append(f"{field_spec.label} must be a number")
                continue
            if not math.isfinite(number):
                issues.append(f"{field_spec.label} has a non-finite value")
                continue
            if field_spec.field_type == "int" and not number.is_integer():
                issues.append(f"{field_spec.label} must be an integer")
                continue
            if field_spec.field_type == "float":
                try:
                    decimals = command_decimals(command, field_spec.name)
                    if (
                        command.type in {CommandType.SET_TEMPERATURE, CommandType.SCAN_TEMPERATURE}
                        and field_spec.name in {"target", "start", "stop"}
                    ):
                        # The SEQ file is portable; the installed controller checks its own resolution.
                        decimals = 4
                    require_precision(number, decimals, field_spec.label)
                except ValueError as exc:
                    issues.append(str(exc))
            if field_spec.minimum is not None and number < field_spec.minimum:
                issues.append(
                    f"{field_spec.label} must be at least {field_spec.minimum:g}"
                )
            if field_spec.maximum is not None and number > field_spec.maximum:
                issues.append(
                    f"{field_spec.label} must be no more than {field_spec.maximum:g}"
                )
        elif field_spec.field_type == "choice" and field_spec.choices:
            normalized = str(value).casefold()
            if normalized not in {
                choice.casefold() for choice in field_spec.choices
            }:
                issues.append(
                    f"{field_spec.label} must be one of "
                    + ", ".join(field_spec.choices)
                )
        elif field_spec.field_type == "bool" and not isinstance(value, bool):
            issues.append(f"{field_spec.label} must be true or false")
    if (
        command.type is CommandType.SCAN_TEMPERATURE
        and str(command.params.get("point_mode", "Linear")).casefold() == "log"
    ):
        try:
            start = float(command.params.get("start", 0.0))
            stop = float(command.params.get("stop", 0.0))
        except (TypeError, ValueError):
            pass
        else:
            if math.isfinite(start) and math.isfinite(stop) and (start <= 0.0 or stop <= 0.0):
                issues.append("Scan Temperature Log mode requires positive Start and Stop temperatures")
    return tuple(issues)
