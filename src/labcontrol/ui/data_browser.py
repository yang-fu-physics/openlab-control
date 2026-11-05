"""与当前测量解耦的 DAT 浏览器、自动刷新和点详情窗口。

浏览器只跟踪用户明确打开或拖入的文件，不会因新 SEQ 启动而自动改绑。750 ms 定时器比较
修改纳秒和文件大小，仅在签名变化时重新读取；读取器记录实际解析字节数，因此与写入并发时
会在下一轮补读。重载保留当前坐标视图，切换文件才应用同名 PLT。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, QThreadPool, Qt, QTimer, Signal, Slot
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
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..data_filter import DataFilter
from ..dat_reader import DatDocument, DatPoint, DatReadError, read_dat, read_raw_dat
from ..plot_format import (
    LOG_SCALE,
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
from ..time_filter import time_filter_context
from .data_filter_dialog import DataFilterDialog
from .scaling import scaled
from .window_sizing import fit_initial_window_width


class _FileReadSignals(QObject):
    finished = Signal(object, object, int)


class _FileReadTask(QRunnable):
    """Perform a read without ever touching Qt widgets from the worker."""

    def __init__(self, read: Callable[[], DatDocument | str], generation: int) -> None:
        super().__init__()
        self.read = read
        self.generation = generation
        self.signals = _FileReadSignals()

    def run(self) -> None:
        try:
            result = self.read()
            if isinstance(result, DatDocument):
                result.numeric_columns()
        except (DatReadError, OSError) as exc:
            self.signals.finished.emit(None, exc, self.generation)
        else:
            self.signals.finished.emit(result, None, self.generation)


class PointDetailsDialog(QDialog):
    """显示一个绘图点对应的完整 DAT 行，而不只显示 X/Y。"""

    def __init__(
        self,
        document: DatDocument,
        hit: PlotHit,
        x_label: str,
        x_value_text: str | None = None,
        parent: QWidget | None = None,
        *,
        points: tuple[DatPoint, ...] = (),
        format_x: Callable[[float], str] | None = None,
        select: Callable[[PlotHit], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._document = document
        self._series = hit.series
        self._x_label = x_label
        self._format_x = format_x
        self._select = select
        self._points = points or (hit.point,)
        self._index = bisect_left(self._points, hit.row_index, key=lambda point: point.row_index)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.table = table = QTableWidget(len(document.columns), 2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        for row, column in enumerate(document.columns):
            table.setItem(row, 0, QTableWidgetItem(column))
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.previous_button = buttons.addButton("Previous Point", QDialogButtonBox.ButtonRole.ActionRole)
        self.next_button = buttons.addButton("Next Point", QDialogButtonBox.ButtonRole.ActionRole)
        self.previous_button.clicked.connect(lambda: self._navigate(-1))
        self.next_button.clicked.connect(lambda: self._navigate(1))
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
        self._render_point(x_value_text)
        fit_initial_window_width(
            self,
            preferred_height=scaled(470),
        )

    def _render_point(self, x_value_text: str | None = None) -> None:
        point = self._points[self._index]
        x = x_value_text if x_value_text is not None else (
            self._format_x(point.x) if self._format_x is not None else f"{point.x:.12g}"
        )
        self.setWindowTitle(f"Data Point Details - Row {point.row_index + 1}")
        self.summary_label.setText(
            f"File: {self._document.path}\nData row: {point.row_index + 1:,}    "
            f"{self._x_label}: {x}    {self._series}: {point.y:.12g}"
        )
        for row, value in enumerate(point.row):
            self.table.setItem(row, 1, QTableWidgetItem(value))
        self.previous_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index + 1 < len(self._points))

    def _navigate(self, offset: int) -> None:
        self._index += offset
        self._render_point()
        if self._select is not None:
            self._select(PlotHit(self._series, self._points[self._index]))


class DatBrowserWidget(QWidget):
    """只跟踪用户明确打开或拖入文件的独立 DAT 查看器。"""

    fileChanged = Signal(str)

    def __init__(self, start_directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.start_directory = start_directory
        self.current_path: Path | None = None
        self.document: DatDocument | None = None
        self._signature: tuple[int, int] | None = None
        self._suspend_format_save = False
        self._format_status = ""
        self._filter_status = ""
        self._last_auto_save_error: str | None = None
        self._generation = 0
        self._pending_read: tuple[Path, bool] | None = None
        self._raw_generation = 0
        self._raw_dialog: QDialog | None = None
        self._details: PointDetailsDialog | None = None
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
        self.filter_button = QPushButton("Data Filter...")
        self.filter_button.setEnabled(False)
        reset_button = QPushButton("Reset Zoom")
        controls.addWidget(self.path_label, 1)
        controls.addWidget(QLabel("Layout:"))
        controls.addWidget(self.layout_combo)
        controls.addWidget(open_button)
        controls.addWidget(reload_button)
        controls.addWidget(self.filter_button)
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
        self.filter_button.clicked.connect(self.open_data_filter)
        reset_button.clicked.connect(lambda checked=False: self.canvas.reset_zoom())
        self.layout_combo.currentIndexChanged.connect(self._layout_selected)
        self.canvas.openRequested.connect(self.open_dialog)
        self.canvas.reloadRequested.connect(self.reload)
        self.canvas.saveFormatRequested.connect(lambda: self.save_format(show_errors=True))
        self.canvas.reloadFormatRequested.connect(self.reload_format)
        self.canvas.axesChanged.connect(self._update_status)
        self.canvas.displayChanged.connect(self._display_changed)
        self.canvas.pointActivated.connect(self._show_point_details)
        self.canvas.pointsChanged.connect(self._close_point_details)
        self.canvas.filterRequested.connect(self.open_data_filter)
        self.canvas.filterInvalidated.connect(self._filter_invalidated)
        self.canvas.rawFileRequested.connect(self.browse_raw_file)

        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(750)
        self.monitor_timer.timeout.connect(self._check_for_updates)
        self.monitor_timer.start()

    def canvas_reset_zoom(self) -> None:
        """供父窗口菜单转发 Reset Zoom。"""

        self.canvas.reset_zoom()

    def open_dialog(self) -> None:
        """从当前文件目录或配置运行目录选择任意 DAT。"""

        directory = self.current_path.parent if self.current_path is not None else self.start_directory
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open DAT File",
            str(directory),
            "DAT data (*.dat);;All files (*)",
        )
        if selected:
            self.queue_load(selected)

    def load_path(self, path: str | Path, show_errors: bool = True) -> bool:
        """读取一个 DAT；刷新同一文件时保留视图，切换文件时加载其 PLT。"""

        source = Path(path).resolve()
        self._generation += 1
        self._pending_read = None
        try:
            document = read_dat(source)
        except (DatReadError, OSError) as exc:
            self._report_read_error(source, exc, show_errors)
            return False
        self._commit_document(document, show_errors)
        return True

    def queue_load(
        self, path: str | Path, *, show_errors: bool = True, refresh: bool = False,
    ) -> None:
        source = Path(path).resolve()
        self._generation += 1
        self._pending_read = (source, show_errors)
        if not refresh:
            self.status_label.setText(f"Reading {source.name}…")
        task = _FileReadTask(
            lambda: read_dat(source, allow_unterminated_last_line=not refresh),
            self._generation,
        )
        task.signals.finished.connect(self._finish_load, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(task)

    @Slot(object, object, int)
    def _finish_load(self, document: DatDocument | None, error: Exception | None, generation: int) -> None:
        if generation != self._generation:
            return
        source, show_errors = self._pending_read
        self._pending_read = None
        if error is not None:
            self._report_read_error(source, error, show_errors)
        else:
            self._commit_document(document, show_errors)

    def _report_read_error(self, source: Path, error: Exception, show_errors: bool) -> None:
        self.status_label.setText(f"Unable to read {source.name}: {error}")
        if show_errors:
            QMessageBox.warning(self, "Unable to Open DAT", str(error))

    def _commit_document(self, document: DatDocument, show_errors: bool) -> None:
        source = document.path
        new_file = self.current_path != source or self.document is None
        preserve_view = not new_file
        self.current_path = source
        self.document = document
        self._signature = (document.modified_ns, document.size_bytes)
        self.path_label.setText(str(source))
        self.filter_button.setEnabled(True)
        if new_file:
            self._format_status = ""
            self._filter_status = ""
            self._raw_generation += 1

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
    def reload(self) -> None:
        """手动重读当前文件；尚未选择时打开文件对话框。"""

        if self.current_path is None:
            self.open_dialog()
        else:
            self.queue_load(self.current_path)

    def save_format(self, show_errors: bool = True) -> bool:
        """保存当前列、布局、坐标类型和缩放范围；自动保存错误只提示一次。"""

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
        """重新应用当前 DAT 旁的 PLT，不重新读取数据文件。"""

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
        """响应绘图交互，并在非批量应用阶段自动保存 PLT。"""

        self._sync_layout_control()
        if not self._suspend_format_save:
            self.save_format(show_errors=False)
        self._update_status(self.canvas.x_label, self.canvas.y_columns)

    def _check_for_updates(self) -> None:
        """通过文件签名变化触发无弹窗刷新；文件暂时不可用时等待重试。"""

        if self.current_path is None or self._pending_read is not None:
            return
        try:
            stat = self.current_path.stat()
        except OSError:
            self.status_label.setText(f"File unavailable; waiting to retry: {self.current_path}")
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != self._signature:
            self.queue_load(self.current_path, show_errors=False, refresh=True)

    def open_data_filter(self) -> None:
        if self.document is None:
            return
        dialog = DataFilterDialog(
            self.document.numeric_columns(), self.canvas.data_filter, self,
            apply_callback=self._apply_data_filter,
            time_context=time_filter_context(self.document),
        )
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _apply_data_filter(self, data_filter: DataFilter) -> None:
        self._filter_status = ""
        self.canvas.set_data_filter(data_filter)
        self._update_status(self.canvas.x_label, self.canvas.y_columns)

    def _filter_invalidated(self, columns: tuple[str, ...]) -> None:
        self._filter_status = "Filters cleared for unavailable columns: " + ", ".join(columns)

    def browse_raw_file(self) -> None:
        source = self.current_path
        if source is None:
            return
        self._raw_generation += 1
        task = _FileReadTask(lambda: read_raw_dat(source), self._raw_generation)
        task.signals.finished.connect(self._finish_raw_read, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(task)

    @Slot(object, object, int)
    def _finish_raw_read(self, text: str | None, error: Exception | None, generation: int) -> None:
        if generation != self._raw_generation:
            return
        if error is not None:
            QMessageBox.warning(self, "Unable to Read Raw DAT", str(error))
            return
        if self._raw_dialog is not None:
            self._raw_dialog.close()
        dialog = QDialog(self)
        self._raw_dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(f"Raw File (read-only) - {self.current_path.name}")
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(text)
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        dialog.finished.connect(self._raw_dialog_closed)
        dialog.resize(scaled(820), scaled(560))
        dialog.show()

    @Slot(int)
    def _raw_dialog_closed(self, _result: int) -> None:
        self._raw_dialog = None

    def _update_status(self, x_label: str, y_columns: object) -> None:
        if self.document is None:
            return
        names = tuple(str(name) for name in y_columns) if isinstance(y_columns, (tuple, list)) else (str(y_columns),)
        y_text = ", ".join(names) if names else "None"
        layout = "Overlay" if self.canvas.layout_mode == OVERLAY_LAYOUT else "Stacked shared X"
        format_text = f" | {self._format_status}" if self._format_status else ""
        filter_text = (
            f" | Filtered: {self.canvas.filter_match_count:,}/{len(self.document.rows):,} rows"
            if self.canvas.data_filter.is_active else ""
        )
        if self._filter_status:
            filter_text += f" | {self._filter_status}"
        self.status_label.setText(
            f"{len(self.document.rows):,} rows | "
            f"X: {x_label} [{self.canvas.x_scale.title()}] | "
            f"Y: {y_text} [{self.canvas.y_scale.title()}] | {layout} | "
            f"refreshed {datetime.now().strftime('%H:%M:%S')} | auto-refresh 0.75 s{format_text}{filter_text}"
        )

    def _show_point_details(self, hit: PlotHit) -> None:
        """用临时模态窗口展示完整行，并在关闭后安排 Qt 对象释放。"""

        if self.document is None:
            return
        points = self.canvas.points_by_series[hit.series]
        if LOG_SCALE in (self.canvas.x_scale, self.canvas.y_scale):
            points = tuple(point for point in points if self.canvas._point_is_plottable(point))
        dialog = PointDetailsDialog(
            self.document,
            hit,
            self.canvas.x_label,
            self.canvas.format_x_value(
                hit.x,
                full=True,
            ),
            self,
            points=points,
            format_x=lambda value: self.canvas.format_x_value(value, full=True),
            select=self.canvas._select_point,
        )
        self._details = dialog
        try:
            dialog.exec()
        finally:
            self._details = None
            dialog.deleteLater()

    def _close_point_details(self) -> None:
        if self._details is not None:
            self._details.reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.monitor_timer.stop()
        self._generation += 1
        self._raw_generation += 1
        self._pending_read = None
        self._close_point_details()
        if self._raw_dialog is not None:
            self._raw_dialog.close()
        super().closeEvent(event)

    @staticmethod
    def _first_dat_path(event: QDragEnterEvent | QDropEvent) -> Path | None:
        """从拖放 URL 中选择第一份现存本地 DAT。"""

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
        if path is not None:
            self.queue_load(path)
            event.acceptProposedAction()
        else:
            event.ignore()
