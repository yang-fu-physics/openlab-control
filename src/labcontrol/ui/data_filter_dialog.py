"""English five-row data-filter editor with a fixed time row."""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QDateTime, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDateTimeEdit,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..data_filter import DataFilter, DataFilterRow, FILTER_ROW_COUNT
from ..time_filter import (
    TimeFilterContext,
    format_time_bound,
    parse_time_bound,
)
from .scaling import scaled
from .window_sizing import fit_initial_window_width


class TimeBoundEdit(QDateTimeEdit):
    """A clearable wall-time editor with a millisecond display."""

    DISPLAY_FORMAT = "yyyy-MM-dd HH:mm:ss.zzz"

    def __init__(self, context: TimeFilterContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setCalendarPopup(True)
        self.setDisplayFormat(self.DISPLAY_FORMAT)
        # Qt needs a non-empty special value to avoid restoring a date when the
        # bound is cleared; the single space is visually blank and parses as
        # an unbounded value.
        self.setSpecialValueText(" ")
        self._empty_value = self.minimumDateTime()
        self.setDateTime(self._empty_value)
        self.lineEdit().clear()
        self.lineEdit().setPlaceholderText("Unbounded")
        self.lineEdit().setClearButtonEnabled(True)

    def set_value(self, value: float | None) -> None:
        if value is None:
            self.setDateTime(self._empty_value)
            self.lineEdit().clear()
            return
        text = format_time_bound(value, self.context)
        self.setDateTime(QDateTime.fromString(text, self.DISPLAY_FORMAT))

    def clear_value(self) -> None:
        self.setDateTime(self._empty_value)
        self.lineEdit().clear()

    def value(self) -> float | None:
        if self.dateTime() == self._empty_value or not self.lineEdit().text().strip():
            return None
        return parse_time_bound(self.lineEdit().text(), self.context)


class DurationBoundEdit(QLineEdit):
    """A clearable elapsed-duration editor."""

    def __init__(self, context: TimeFilterContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setPlaceholderText("Unbounded")
        self.setClearButtonEnabled(True)

    def set_value(self, value: float | None) -> None:
        self.setText("" if value is None else format_time_bound(value, self.context))

    def value(self) -> float | None:
        return parse_time_bound(self.text(), self.context)


class DataFilterDialog(QDialog):
    """Edit one fixed time row and four general rows without enable toggles."""

    filterApplied = Signal(object)

    def __init__(
        self,
        numeric_columns: Sequence[str],
        data_filter: DataFilter | None = None,
        parent: QWidget | None = None,
        apply_callback: Callable[[DataFilter], object] | None = None,
        time_context: TimeFilterContext | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Data Filter")
        self.setMinimumWidth(scaled(560))
        self._apply_callback = apply_callback
        self._time_context = time_context
        self._special_time_row = time_context is not None
        current = data_filter or DataFilter.empty()
        if self._special_time_row:
            current = current.reserve_time_column(time_context.column)
        self._overflow_rows = current.overflow_rows

        layout = QVBoxLayout(self)
        hint = QLabel(
            time_context.help_text
            if time_context is not None
            else (
                "A row filters when Column and at least one bound are provided. "
                "Rows are combined with AND; blank bounds are unbounded."
            )
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.addWidget(QLabel("Row"), 0, 0)
        grid.addWidget(QLabel("Column"), 0, 1)
        grid.addWidget(QLabel("Min"), 0, 2)
        grid.addWidget(QLabel("Max"), 0, 3)

        self.column_combos: list[QComboBox | None] = []
        self.minimum_edits: list[QLineEdit] = []
        self.maximum_edits: list[QLineEdit] = []
        self.time_column_label: QLabel | None = None
        self.time_minimum_edit: TimeBoundEdit | None = None
        self.time_maximum_edit: TimeBoundEdit | None = None

        for row_index in range(FILTER_ROW_COUNT):
            row = current.rows[row_index]
            if (
                self._special_time_row
                and row_index == 0
                and time_context.column is not None
                and row.column is None
                and not row.has_bounds
                and time_context.initial_minimum is not None
            ):
                row = DataFilterRow(
                    enabled=True,
                    column=time_context.column,
                    minimum=time_context.initial_minimum,
                )
            if self._special_time_row and row_index == 0:
                row_label = "Time"
            elif self._special_time_row:
                row_label = str(row_index)
            else:
                row_label = str(row_index + 1)
            grid.addWidget(QLabel(row_label), row_index + 1, 0)

            if self._special_time_row and row_index == 0:
                column_label = QLabel(
                    "Unavailable" if time_context.column is None else time_context.column
                )
                column_label.setToolTip(time_context.help_text)
                self.time_column_label = column_label
                self.column_combos.append(None)
                grid.addWidget(column_label, row_index + 1, 1)
                minimum = self._make_time_edit(time_context)
                maximum = self._make_time_edit(time_context)
                minimum.set_value(row.minimum)
                maximum.set_value(row.maximum)
                self.time_minimum_edit = minimum
                self.time_maximum_edit = maximum
                self.minimum_edits.append(minimum)
                self.maximum_edits.append(maximum)
                if time_context.column is None:
                    column_label.setStyleSheet("color: #657080;")
                    minimum.setEnabled(False)
                    maximum.setEnabled(False)
                grid.addWidget(minimum, row_index + 1, 2)
                grid.addWidget(maximum, row_index + 1, 3)
                continue

            available_columns = tuple(numeric_columns)
            if self._special_time_row and time_context.column is not None:
                available_columns = tuple(
                    name for name in available_columns if name != time_context.column
                )
            column = QComboBox()
            column.addItem("None", None)
            for name in available_columns:
                column.addItem(str(name), str(name))
            selected_index = column.findData(row.column)
            if selected_index < 0 and row.column is not None:
                column.addItem("%s (unavailable)" % row.column, row.column)
                selected_index = column.findData(row.column)
            column.setCurrentIndex(max(0, selected_index))
            self.column_combos.append(column)
            grid.addWidget(column, row_index + 1, 1)

            minimum = QLineEdit()
            minimum.setPlaceholderText("Unbounded")
            if row.minimum is not None:
                minimum.setText(repr(row.minimum))
            self.minimum_edits.append(minimum)
            grid.addWidget(minimum, row_index + 1, 2)

            maximum = QLineEdit()
            maximum.setPlaceholderText("Unbounded")
            if row.maximum is not None:
                maximum.setText(repr(row.maximum))
            self.maximum_edits.append(maximum)
            grid.addWidget(maximum, row_index + 1, 3)

        layout.addLayout(grid)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b3261e;")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.clear_button = QPushButton("Clear")
        self.apply_button = QPushButton("Apply")
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self.clear_button.clicked.connect(self.clear)
        self.apply_button.clicked.connect(self.apply)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        fit_initial_window_width(self, preferred_height=scaled(340))

    @staticmethod
    def _make_time_edit(context: TimeFilterContext) -> QWidget:
        if context.is_wall_time:
            return TimeBoundEdit(context)
        return DurationBoundEdit(context)

    def _bound(self, edit: QLineEdit, label: str, row_number: int) -> float | None:
        text = edit.text().strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(
                "Filter row %d: %s must be a finite number" % (row_number, label)
            ) from exc
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(
                "Filter row %d: %s must be a finite number" % (row_number, label)
            )
        return value

    def _time_bound(self, edit: QLineEdit, label: str) -> float | None:
        try:
            return edit.value()
        except ValueError as exc:
            raise ValueError("Time row %s: %s" % (label, exc)) from exc

    def _read_filter(self) -> DataFilter:
        rows = []
        if self._special_time_row:
            if self._time_context.column is None:
                rows.append(DataFilterRow())
            else:
                minimum = self._time_bound(self.time_minimum_edit, "Min")
                maximum = self._time_bound(self.time_maximum_edit, "Max")
                try:
                    rows.append(
                        DataFilterRow(
                            enabled=minimum is not None or maximum is not None,
                            column=self._time_context.column,
                            minimum=minimum,
                            maximum=maximum,
                        )
                    )
                except ValueError as exc:
                    raise ValueError("Time row: %s" % exc) from exc
            start_index = 1
        else:
            start_index = 0

        for index in range(start_index, FILTER_ROW_COUNT):
            row_number = index + 1
            minimum = self._bound(self.minimum_edits[index], "Min", row_number)
            maximum = self._bound(self.maximum_edits[index], "Max", row_number)
            column = self.column_combos[index].currentData()
            if column is None and (minimum is not None or maximum is not None):
                raise ValueError(
                    "Filter row %d: choose a data column or clear Min and Max"
                    % row_number
                )
            try:
                rows.append(
                    DataFilterRow(
                        enabled=column is not None
                        and (minimum is not None or maximum is not None),
                        column=column,
                        minimum=minimum,
                        maximum=maximum,
                    )
                )
            except ValueError as exc:
                raise ValueError("Filter row %d: %s" % (row_number, exc)) from exc
        return DataFilter(tuple(rows), self._overflow_rows)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)

    def _commit(self) -> bool:
        try:
            data_filter = self._read_filter()
        except ValueError as exc:
            self._show_error(str(exc))
            return False
        if self._apply_callback is not None:
            try:
                applied = self._apply_callback(data_filter)
            except ValueError as exc:
                self._show_error("Filter not applied: %s" % exc)
                return False
            if applied is False:
                self._show_error("Filter not applied")
                return False
        self.filterApplied.emit(data_filter)
        self.error_label.clear()
        return True

    def apply(self) -> None:
        """Commit valid edits while keeping this dialog open."""

        self._commit()

    def accept(self) -> None:  # noqa: N802
        if self._commit():
            super().accept()

    def clear(self) -> None:
        """Clear the editor; the applied filter changes on Apply or OK."""

        self._overflow_rows = ()

        for index, (column, minimum, maximum) in enumerate(
            zip(
                self.column_combos,
                self.minimum_edits,
                self.maximum_edits,
            )
        ):
            if column is not None and not (
                self._special_time_row and index == 0
            ):
                column.setCurrentIndex(0)
            if isinstance(minimum, QDateTimeEdit):
                minimum.clear_value()
            else:
                minimum.clear()
            if isinstance(maximum, QDateTimeEdit):
                maximum.clear_value()
            else:
                maximum.clear()
        self.error_label.clear()
