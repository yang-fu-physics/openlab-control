"""与当前测量解耦的 DAT 浏览器、自动刷新和点详情窗口。

浏览器只跟踪用户明确打开或拖入的文件，不会因新 SEQ 启动而自动改绑。750 ms 定时器比较
修改纳秒和文件大小，仅在签名变化时重新读取；读取器记录实际解析字节数，因此与写入并发时
会在下一轮补读。重载保留当前坐标视图，切换文件才应用同名 PLT。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QToolBar,
    QSizePolicy,
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
from .window_sizing import fit_initial_window_width


class PointDetailsDialog(QDialog):
    """显示一个绘图点对应的完整 DAT 行，而不只显示 X/Y。"""

    def __init__(
        self,
        document: DatDocument,
        hit: PlotHit,
        x_label: str,
        x_value_text: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        point = hit.point
        self.setWindowTitle(f"Data Point Details - Row {point.row_index + 1}")
        layout = QVBoxLayout(self)
        displayed_x = (
            x_value_text
            if x_value_text is not None
            else f"{point.x:.12g}"
        )
        self.summary_label = QLabel(
            f"File: {document.path}\n"
            f"Data row: {point.row_index + 1:,}    "
            f"{x_label}: {displayed_x}    "
            f"{hit.series}: {point.y:.12g}"
        )
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
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
        fit_initial_window_width(
            self,
            preferred_height=scaled(470),
        )


class DatBrowserWidget(QWidget):
    """只跟踪用户明确打开或拖入文件的独立 DAT 查看器。"""

    fileChanged = Signal(str)
    detachToggled = Signal()

    def __init__(self, start_directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.start_directory = start_directory
        self.current_path: Path | None = None
        self.document: DatDocument | None = None
        self._signature: tuple[int, int] | None = None
        self._suspend_format_save = False
        self._format_status = ""
        self._base_status_text = "Waiting for a DAT file"
        self._hover_hit: PlotHit | None = None
        self._last_auto_save_error: str | None = None
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Keep path_label for programmatic inspection without spending vertical
        # canvas height on a wrapped two-line path header; the window title and
        # tooltip surface the filename and full path cleanly.
        self.path_label = QLabel("No DAT file selected")
        self.path_label.hide()

        icon_color = "#334155"
        controls = QToolBar(self)
        controls.setMovable(False)
        controls.setFloatable(False)
        controls.setIconSize(QSize(scaled(13), scaled(13)))
        controls.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        controls.setStyleSheet(
            "QToolBar { background: #f4f6f8; border: none; "
            "border-bottom: 1px solid #d0d7de; padding: 2px 4px; spacing: 2px; }"
            "QToolButton { padding: 3px 6px; border-radius: 3px; color: #1f2630; }"
            "QToolButton:hover { background: #e6ebf1; }"
        )
        self.layout_combo = QComboBox()
        self.layout_combo.setToolTip("Overlay Y series or stack plots with a shared X axis")
        self.layout_combo.addItem("Overlay", OVERLAY_LAYOUT)
        self.layout_combo.addItem("Stacked", STACKED_LAYOUT)
        controls.addWidget(self.layout_combo)
        controls.addSeparator()
        open_action = controls.addAction(
            qta.icon("fa5s.folder-open", color=icon_color),
            "Open",
        )
        open_action.setToolTip("Open DAT or System Log file")
        y_series_action = controls.addAction(
            qta.icon("fa5s.list-ul", color=icon_color),
            "Y Series",
        )
        y_series_action.setToolTip("Select Y series to plot")
        reload_action = controls.addAction(
            qta.icon("fa5s.sync-alt", color=icon_color),
            "Reload",
        )
        reload_action.setToolTip("Reload current DAT file")
        self.save_format_button = controls.addAction(
            qta.icon("fa5s.save", color=icon_color),
            "Save PLT",
        )
        self.save_format_button.setToolTip("Save plot format (.plt)")
        reset_action = controls.addAction(
            qta.icon("fa5s.compress-arrows-alt", color=icon_color),
            "Reset Zoom",
        )
        reset_action.setToolTip("Reset zoom to fit all points")
        spacer = QWidget(controls)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        controls.addWidget(spacer)
        self.detach_action = controls.addAction(
            qta.icon("fa5s.external-link-alt", color=icon_color),
            "Pop Out",
        )
        for text_action in (y_series_action, self.detach_action):
            btn = controls.widgetForAction(text_action)
            if btn is not None:
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._detached = False
        self.set_detached(False)
        layout.addWidget(controls)

        self.canvas = DatPlotCanvas()
        layout.addWidget(self.canvas, 1)

        self.status_label = QLabel(self._base_status_text)
        self.status_label.setStyleSheet(
            "QLabel { background: #f4f6f8; color: #475569; "
            "border-top: 1px solid #d0d7de; padding: 2px 8px; }"
        )
        self.status_label.setWordWrap(False)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.status_label)

        open_action.triggered.connect(self.open_dialog)
        y_series_action.triggered.connect(lambda checked=False: self.canvas.select_y_series())
        reload_action.triggered.connect(self.reload)
        self.save_format_button.triggered.connect(lambda checked=False: self.save_format(show_errors=True))
        reset_action.triggered.connect(lambda checked=False: self.canvas.reset_zoom())
        self.detach_action.triggered.connect(lambda checked=False: self.detachToggled.emit())
        self.layout_combo.currentIndexChanged.connect(self._layout_selected)
        self.canvas.openRequested.connect(self.open_dialog)
        self.canvas.reloadRequested.connect(self.reload)
        self.canvas.saveFormatRequested.connect(lambda: self.save_format(show_errors=True))
        self.canvas.reloadFormatRequested.connect(self.reload_format)
        self.canvas.copyPathRequested.connect(self._copy_current_path)
        self.canvas.axesChanged.connect(self._update_status)
        self.canvas.displayChanged.connect(self._display_changed)
        self.canvas.pointActivated.connect(self._show_point_details)
        self.canvas.pointHovered.connect(self._on_point_hovered)

        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(750)
        self.monitor_timer.timeout.connect(self._check_for_updates)
        self.monitor_timer.start()

    def set_detached(self, detached: bool) -> None:
        """同步工具栏的独立窗口/放回主窗口按钮状态。"""

        self._detached = bool(detached)
        if self._detached:
            self.detach_action.setIcon(qta.icon("fa5s.compress", color="#334155"))
            self.detach_action.setText("Dock")
            self.detach_action.setToolTip(
                "Return Data Browser to the main window central area"
            )
        else:
            self.detach_action.setIcon(qta.icon("fa5s.external-link-alt", color="#334155"))
            self.detach_action.setText("Pop Out")
            self.detach_action.setToolTip(
                "Detach Data Browser into an independent window "
                "(or drag its title bar outside the central area)"
            )

    def _copy_current_path(self) -> None:
        if self.current_path is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(self.current_path))

    def canvas_reset_zoom(self) -> None:
        """供父窗口菜单转发 Reset Zoom。"""

        self.canvas.reset_zoom()

    def open_dialog(self) -> None:
        """从当前文件目录或配置运行目录选择任意 DAT。"""

        directory = self.current_path.parent if self.current_path is not None else self.start_directory
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open DAT or System Log File",
            str(directory),
            "Data files (*.dat *.log);;DAT data (*.dat);;Log files (*.log);;All files (*)",
        )
        if selected:
            self.load_path(selected, show_errors=True)

    def load_path(self, path: str | Path, show_errors: bool = True) -> bool:
        """读取一个 DAT；刷新同一文件时保留视图，切换文件时加载其 PLT。"""

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
        self.setToolTip(str(source))

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
        """手动重读当前文件；尚未选择时打开文件对话框。"""

        if self.current_path is None:
            self.open_dialog()
        else:
            self.load_path(self.current_path, show_errors=True)

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

    def _render_status_bar(self) -> None:
        if self._hover_hit is not None and self.document is not None:
            x_text = self.canvas.format_x_value(self._hover_hit.x, full=False)
            self.status_label.setText(
                f"{self.canvas.x_label} = {x_text}  ·  "
                f"{self._hover_hit.series} = {self._hover_hit.y:.8g}  "
                f"(row {self._hover_hit.row_index + 1:,})   |   {self._base_status_text}"
            )
        else:
            self.status_label.setText(self._base_status_text)

    def _on_point_hovered(self, hit: object) -> None:
        self._hover_hit = hit if isinstance(hit, PlotHit) else None
        self._render_status_bar()

    def _update_status(self, x_label: str, y_columns: object) -> None:
        if self.document is None:
            return
        names = tuple(str(name) for name in y_columns) if isinstance(y_columns, (tuple, list)) else (str(y_columns),)
        y_text = ", ".join(names) if names else "None"
        self._base_status_text = (
            f"{len(self.document.rows):,} pts · "
            f"X: {x_label} [{self.canvas.x_scale.title()}] · "
            f"Y: {y_text} [{self.canvas.y_scale.title()}] · "
            f"{datetime.now().strftime('%H:%M:%S')}"
        )
        details = [str(self.current_path) if self.current_path else "", self._base_status_text]
        if self._format_status:
            details.append(self._format_status)
        details.append("Drag to zoom · Double-click point for full row · Auto-refresh 0.75 s")
        self.status_label.setToolTip("\n".join(line for line in details if line))
        self._render_status_bar()

    def _show_point_details(self, hit: PlotHit) -> None:
        """用临时模态窗口展示完整行，并在关闭后安排 Qt 对象释放。"""

        if self.document is None:
            return
        dialog = PointDetailsDialog(
            self.document,
            hit,
            self.canvas.x_label,
            self.canvas.format_x_value(
                hit.x,
                full=True,
            ),
            self,
        )
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    @staticmethod
    def _first_dat_path(event: QDragEnterEvent | QDropEvent) -> Path | None:
        """从拖放 URL 中选择第一份现存本地 DAT。"""

        if not event.mimeData().hasUrls():
            return None
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_file() and path.suffix.casefold() in {".dat", ".log"}:
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
