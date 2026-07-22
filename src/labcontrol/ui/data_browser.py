from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..dat_reader import DatDocument, DatReadError, read_dat
from ..plot_format import (
    PlotFormatError,
    find_plot_format,
    load_plot_format,
    save_plot_format,
)
from .dat_plot import (
    OVERLAY_LAYOUT,
    STACKED_LAYOUT,
    DatPlotCanvas,
    PlotHit,
)
from .scaling import scaled


class PointDetailsDialog(QDialog):
    def __init__(
        self,
        document: DatDocument,
        hit: PlotHit,
        x_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        point = hit.point
        self.setWindowTitle(f"Data Point Details - Row {point.row_index + 1}")
        self.resize(scaled(620), scaled(470))
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"File: {document.path}\n"
            f"Data row: {point.row_index + 1:,}    {x_label}: {point.x:.12g}    "
            f"{hit.series}: {point.y:.12g}"
        )
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        table = QTableWidget(len(document.columns), 2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        for row, (column, value) in enumerate(zip(document.columns, point.row, strict=True)):
            table.setItem(row, 0, QTableWidgetItem(column))
            table.setItem(row, 1, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)


class DatBrowserWidget(QWidget):
    """Independent DAT viewer following only the explicitly opened/dropped file."""

    fileChanged = Signal(str)

    def __init__(self, start_directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.start_directory = start_directory
        self.current_path: Path | None = None
        self.document: DatDocument | None = None
        self._signature: tuple[int, int] | None = None
        self._suspend_format_save = False
        self._format_status = ""
        self._last_auto_save_error: str | None = None
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.path_label = QLabel("No DAT file selected - drop a file anywhere in this window")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setWordWrap(True)
        self.layout_combo = QComboBox()
        self.layout_combo.setToolTip("Overlay Y series or stack plots with a shared X axis")
        self.layout_combo.addItem("Overlay", OVERLAY_LAYOUT)
        self.layout_combo.addItem("Stacked / Shared X", STACKED_LAYOUT)
        open_button = QPushButton("Open DAT")
        reload_button = QPushButton("Reload")
        self.save_format_button = QPushButton("Save PLT")
        reset_button = QPushButton("Reset Zoom")
        controls.addWidget(self.path_label, 1)
        controls.addWidget(QLabel("Layout:"))
        controls.addWidget(self.layout_combo)
        controls.addWidget(open_button)
        controls.addWidget(reload_button)
        controls.addWidget(self.save_format_button)
        controls.addWidget(reset_button)
        layout.addLayout(controls)

        self.canvas = DatPlotCanvas()
        layout.addWidget(self.canvas, 1)
        self.status_label = QLabel("Waiting for a DAT file")
        self.status_label.setStyleSheet("color: #5d6672;")
        layout.addWidget(self.status_label)

        open_button.clicked.connect(self.open_dialog)
        reload_button.clicked.connect(self.reload)
        self.save_format_button.clicked.connect(lambda checked=False: self.save_format(show_errors=True))
        reset_button.clicked.connect(lambda checked=False: self.canvas.reset_zoom())
        self.layout_combo.currentIndexChanged.connect(self._layout_selected)
        self.canvas.openRequested.connect(self.open_dialog)
        self.canvas.reloadRequested.connect(self.reload)
        self.canvas.saveFormatRequested.connect(lambda: self.save_format(show_errors=True))
        self.canvas.reloadFormatRequested.connect(self.reload_format)
        self.canvas.axesChanged.connect(self._update_status)
        self.canvas.displayChanged.connect(self._display_changed)
        self.canvas.pointActivated.connect(self._show_point_details)

        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(750)
        self.monitor_timer.timeout.connect(self._check_for_updates)
        self.monitor_timer.start()

    def canvas_reset_zoom(self) -> None:
        self.canvas.reset_zoom()

    def open_dialog(self) -> None:
        directory = self.current_path.parent if self.current_path is not None else self.start_directory
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open DAT File",
            str(directory),
            "DAT data (*.dat);;All files (*)",
        )
        if selected:
            self.load_path(selected, show_errors=True)

    def load_path(self, path: str | Path, show_errors: bool = True) -> bool:
        source = Path(path).resolve()
        try:
            document = read_dat(source)
        except (DatReadError, OSError) as exc:
            self.status_label.setText(f"Unable to refresh {source.name}: {exc}")
            if show_errors:
                QMessageBox.warning(self, "Unable to Open DAT", str(exc))
            return False

        new_file = self.current_path != source or self.document is None
        preserve_view = not new_file
        self.current_path = source
        self.document = document
        self._signature = (document.modified_ns, document.size_bytes)
        self.path_label.setText(str(source))

        format_error: str | None = None
        format_loaded = False
        self._suspend_format_save = True
        try:
            self.canvas.set_document(document, preserve_view=preserve_view)
            if new_file:
                settings_path = find_plot_format(source)
                if settings_path is not None:
                    try:
                        self.canvas.apply_plot_format(load_plot_format(settings_path))
                        self._format_status = f"PLT: {settings_path.name} loaded"
                        format_loaded = True
                    except PlotFormatError as exc:
                        format_error = str(exc)
                        self._format_status = f"PLT ignored: {settings_path.name}"
        finally:
            self._suspend_format_save = False

        self._sync_layout_control()
        if new_file and not format_loaded and format_error is None:
            self.save_format(show_errors=False)
        self._update_status(self.canvas.x_label, self.canvas.y_columns)
        self.fileChanged.emit(str(source))
        if format_error is not None and show_errors:
            QMessageBox.warning(
                self,
                "Unable to Apply Plot Format",
                f"The DAT file was loaded, but its PLT settings were ignored.\n\n{format_error}",
            )
        return True

    def reload(self) -> None:
        if self.current_path is None:
            self.open_dialog()
        else:
            self.load_path(self.current_path, show_errors=True)

    def save_format(self, show_errors: bool = True) -> bool:
        if self.current_path is None or not self.canvas.y_columns:
            if show_errors:
                QMessageBox.information(self, "Save Plot Format", "Open a plottable DAT file first.")
            return False
        try:
            settings = self.canvas.to_plot_format(self.current_path.name)
            destination = save_plot_format(self.current_path, settings)
        except PlotFormatError as exc:
            message = str(exc)
            self._format_status = "PLT: save failed"
            if show_errors:
                QMessageBox.warning(self, "Unable to Save Plot Format", message)
            elif message != self._last_auto_save_error:
                self.status_label.setText(message)
            self._last_auto_save_error = message
            return False
        self._last_auto_save_error = None
        self._format_status = f"PLT: {destination.name}"
        if show_errors:
            self._update_status(self.canvas.x_label, self.canvas.y_columns)
        return True

    def reload_format(self) -> None:
        if self.current_path is None:
            QMessageBox.information(self, "Reload Plot Format", "Open a DAT file first.")
            return
        settings_path = find_plot_format(self.current_path)
        if settings_path is None:
            QMessageBox.information(
                self,
                "Reload Plot Format",
                "No matching PLT file was found beside this DAT file.",
            )
            return
        try:
            settings = load_plot_format(settings_path)
            self._suspend_format_save = True
            self.canvas.apply_plot_format(settings)
        except PlotFormatError as exc:
            QMessageBox.warning(self, "Unable to Apply Plot Format", str(exc))
            return
        finally:
            self._suspend_format_save = False
        self._format_status = f"PLT: {settings_path.name} loaded"
        self._sync_layout_control()
        self._update_status(self.canvas.x_label, self.canvas.y_columns)

    def _layout_selected(self, index: int) -> None:
        layout = self.layout_combo.itemData(index)
        if layout:
            self.canvas.set_layout(str(layout))

    def _sync_layout_control(self) -> None:
        index = self.layout_combo.findData(self.canvas.layout_mode)
        if index >= 0:
            blocker = QSignalBlocker(self.layout_combo)
            self.layout_combo.setCurrentIndex(index)
            del blocker

    def _display_changed(self) -> None:
        self._sync_layout_control()
        if not self._suspend_format_save:
            self.save_format(show_errors=False)
        self._update_status(self.canvas.x_label, self.canvas.y_columns)

    def _check_for_updates(self) -> None:
        if self.current_path is None:
            return
        try:
            stat = self.current_path.stat()
        except OSError:
            self.status_label.setText(f"File unavailable; waiting to retry: {self.current_path}")
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != self._signature:
            self.load_path(self.current_path, show_errors=False)

    def _update_status(self, x_label: str, y_columns: object) -> None:
        if self.document is None:
            return
        names = tuple(str(name) for name in y_columns) if isinstance(y_columns, (tuple, list)) else (str(y_columns),)
        y_text = ", ".join(names) if names else "None"
        layout = "Overlay" if self.canvas.layout_mode == OVERLAY_LAYOUT else "Stacked shared X"
        format_text = f" | {self._format_status}" if self._format_status else ""
        self.status_label.setText(
            f"{len(self.document.rows):,} rows | "
            f"X: {x_label} [{self.canvas.x_scale.title()}] | "
            f"Y: {y_text} [{self.canvas.y_scale.title()}] | {layout} | "
            f"refreshed {datetime.now().strftime('%H:%M:%S')} | auto-refresh 0.75 s{format_text}"
        )

    def _show_point_details(self, hit: PlotHit) -> None:
        if self.document is None:
            return
        PointDetailsDialog(self.document, hit, self.canvas.x_label, self).exec()

    @staticmethod
    def _first_dat_path(event: QDragEnterEvent | QDropEvent) -> Path | None:
        if not event.mimeData().hasUrls():
            return None
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_file() and path.suffix.casefold() == ".dat":
                    return path
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if self._first_dat_path(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        path = self._first_dat_path(event)
        if path is not None and self.load_path(path, show_errors=True):
            event.acceptProposedAction()
        else:
            event.ignore()
