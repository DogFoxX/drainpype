"""PySide6 GUI: a single dark window matching the DrainPype layout.

The server-options form is built from the selected server's ``fields``; nothing here
is specific to any one server. Icons are Font Awesome Free (Solid) from
``uploader/assets/fonts/``; plain text symbols are used if the font file is missing.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QItemSelectionModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QSplitterHandle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from .keys import delete_key, load_keys, save_key, saved_keys
from .oauth import OAuthError, access_token, authorize_oauth, disconnect_oauth
from .servers import (
    Field, LoadResult, Server, ServerDefError, add_local_server, add_url_server,
    config_dir, execute_button_action, fetch_choices, render_notes,
    list_server_entries, load_servers, remove_server, render, render_textarea, resolve_url,
)
from .uploader import (
    CurlNotFoundError, DEFAULT_MAX_UPLOADS, MAX_MAX_UPLOADS, MIN_MAX_UPLOADS,
    RUNNING_STATES, ServerBusyError, State, Uploader, human_eta, human_size,
    human_speed,
)

REPO_URL = "https://github.com/DogFoxX/drainpipe"
ASSETS_DIR = Path(__file__).parent / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
SERVER_FIELD_WIDTH = 468
CONTROL_HEIGHT = 30
TEXTAREA_MIN_HEIGHT = 100
TEXTAREA_MAX_HEIGHT = 200

# Font Awesome Free (Solid) code points, with plain-text fallbacks.
ICONS = {
    "plus": ("", "+"),
    "trash": ("", "Del"),
    "play": ("", "▶"),
    "stop": ("", "■"),
    "refresh": ("", "⟳"),
    "restart": ("", "↻"),  # fa-solid fa-rotate-right
    "eye": ("", "Show"),
    "eye_slash": ("", "Hide"),
    "copy": ("", "Copy"),
    "link": ("", "Open"),
    "move_down": ("\uf160", "↓"),
    "move_up": ("\uf161", "↑"),
}

COLUMNS = ["#", "File Name", "Server", "Status", "Size", "Uploaded", "Progress", "Up Speed", "URL", "ETA"]
C_NUMBER, C_NAME, C_SERVER, C_STATUS, C_SIZE, C_SENT, C_PROGRESS, C_SPEED, C_URL, C_ETA = range(10)
COLUMN_WIDTHS = [50, 220, 140, 100, 100, 100, 170, 100, 200, 100]
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 10
KEYS_COLUMN_WIDTHS = [200, 440]
SERVERS_COLUMN_WIDTHS = [170, 320, 210]
ROW_HEIGHT = 23
MIN_VISIBLE_ROWS = 3
DEFAULT_OPTIONS_HEIGHT = 180
MIN_OPTIONS_HEIGHT = 180
MIN_WINDOW_SIZE = (854, 665)
DEFAULT_WINDOW_SIZE = (1150, 820)

STATE_COLORS = {
    State.READY: "#8f99ab",
    State.WAITING: "#8f99ab",
    State.PROCESSING: "#e5a93c",
    State.UPLOADING: "#5b8def",
    State.DONE: "#22c55e",
    State.FAILED: "#ef4444",
}

STYLE_TEMPLATE = """
QWidget { background: #2e3440; color: #eceff4; font-size: 13px; }
QLabel { background: transparent; }
QLabel#hint { color: #7b8394; font-size: 11px; }
QLabel#section { font-weight: bold; }
QFrame#rule { background: #4c566a; min-height: 1px; max-height: 1px; border: none; }
QScrollArea#options_scroll { background: transparent; border: none; }
QScrollArea#options_scroll > QWidget > QWidget { background: transparent; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background: #20242C; border: 1px solid #3b4252; border-radius: 6px; padding: 5px 9px; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #2563d9; }
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #8c95a6; }
QComboBox { padding-right: 30px; }
QComboBox::drop-down { border: none; width: 0px; }
QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
QComboBox QAbstractItemView {
    background: #20242C; border: 1px solid #434c5e; selection-background-color: #2563d9; }
QSpinBox { padding-right: 24px; }
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 20px;
    background: transparent; border: none; outline: none; }
QSpinBox::down-button { subcontrol-position: bottom right; }
QSpinBox QLineEdit { background: transparent; border: none; border-radius: 0px; padding: 0px 6px; }
QSpinBox::up-arrow { image: url("__SPIN_UP_IMAGE__"); width: 18px; height: 14px; }
QSpinBox::down-arrow { image: url("__SPIN_DOWN_IMAGE__"); width: 18px; height: 14px; }
QToolButton#comboChevron {
    background: transparent; border: none; outline: none; padding: 0px; margin: 0px; }
QToolButton#comboChevron:hover, QToolButton#comboChevron:pressed, QToolButton#comboChevron:focus {
    background: transparent; border: none; outline: none; }
QPushButton {
    background: #20242C; border: none; outline: none; border-radius: 8px; padding: 7px 14px; font-weight: bold; }
QPushButton:hover:!disabled { background: #606D87; }
QPushButton:focus { outline: none; }
QPushButton[iconOnly="true"] { padding: 0px; }
QPushButton#add { background: #2563d9; }
QPushButton#add:hover:!disabled { background: #3b76e3; }
QPushButton#remove, QPushButton#stop { background: #dc2626; }
QPushButton#remove:hover:!disabled, QPushButton#stop:hover:!disabled { background: #e53e3e; }
QPushButton#start { background: #22c55e; }
QPushButton#start:hover:!disabled { background: #38d370; }
QPushButton#restart { background: #d97706; }
QPushButton#restart:hover:!disabled { background: #f08a0c; }
QPushButton#move_queue, QPushButton#move_queue:hover:!disabled,
QPushButton#move_queue:pressed { background: #20242C; }
QCheckBox { background: transparent; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; background: #d8dee9; }
QCheckBox::indicator:checked { background: #2563d9; __CHECK_IMAGE__ }
QTableWidget {
    background: #20242C; border: 1px solid #3b4252; border-radius: 6px;
    gridline-color: #2e3440; selection-background-color: #2b4c7e; selection-color: #ffffff; }
QTableWidget::item { padding: 2px 8px; border: none; }
QHeaderView {
    background-color: #20242C; border: none; border-bottom: 1px solid #3b4252;
    border-top-left-radius: 5px; border-top-right-radius: 5px; }
QHeaderView::section {
    background-color: #20242C; border: none; border-right: 1px solid #2e3440;
    border-bottom: 1px solid #3b4252; padding: 6px 10px; text-align: left; }
QTableCornerButton::section {
    background-color: #20242C; border: none; border-bottom: 1px solid #3b4252; }
QScrollBar:horizontal { height: 10px; background: #20242C; border-radius: 5px; }
QScrollBar:vertical { width: 10px; background: #20242C; border-radius: 5px; }
QScrollBar::handle { background: #4c566a; border-radius: 5px; min-width: 24px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
"""


def _ui_state_path() -> Path:
    return config_dir() / "gui.json"


def _load_ui_state() -> dict[str, Any]:
    try:
        raw = _ui_state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_ui_state(updates: dict[str, Any]) -> None:
    try:
        path = _ui_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = _load_ui_state()
        state.update(updates)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _ensure_checkmark_icon() -> str:
    """Render a crisp white checkmark PNG in the temp dir and return its QSS url()."""
    try:
        path = Path(tempfile.gettempdir()) / "drainpype_checkmark.png"
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#ffffff"), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(7, 17, 13, 23)
        painter.drawLine(13, 23, 25, 9)
        painter.end()
        if pixmap.save(str(path), "PNG"):
            return f'image: url("{path.as_posix()}");'
    except Exception:
        pass
    return ""


def _ensure_spin_arrow_icons(icon_family: str | None) -> tuple[str, str]:
    """Render Font Awesome caret-up/down glyphs for Qt's spin-box subcontrols."""
    paths = (
        Path(tempfile.gettempdir()) / "drainpype_spin_up.png",
        Path(tempfile.gettempdir()) / "drainpype_spin_down.png",
    )
    glyphs = ("\uf0d8", "\uf0d7")
    fallbacks = ("▲", "▼")
    for path, glyph, fallback in zip(paths, glyphs, fallbacks):
        pixmap = QPixmap(30, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = QFont(icon_family) if icon_family else QFont()
        font.setPixelSize(20)
        painter.setFont(font)
        painter.setPen(QColor("#eceff4"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph if icon_family else fallback)
        painter.end()
        if not pixmap.save(str(path), "PNG"):
            return "", ""
    return paths[0].as_posix(), paths[1].as_posix()


def _resolve_window_icon() -> QIcon | None:
    candidates = [
        ASSETS_DIR / "icon.ico",
        Path(__file__).resolve().parent.parent / "assets" / "icon.ico",
    ]
    for path in candidates:
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return None


def _apply_disabled_opacity(widget: QWidget) -> None:
    effect = widget.graphicsEffect()
    if widget.isEnabled():
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(1.0)
            effect.setEnabled(False)
    else:
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
        effect.setEnabled(True)
        effect.setOpacity(0.6)


class DisabledOpacityFilter(QObject):
    """Keeps any attached button or input at 60% opacity whenever disabled."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.EnabledChange and isinstance(obj, QWidget):
            _apply_disabled_opacity(obj)
        return False


_OPACITY_FILTER: DisabledOpacityFilter | None = None


def track_disabled_opacity(widget: QWidget) -> QWidget:
    global _OPACITY_FILTER
    if _OPACITY_FILTER is None:
        _OPACITY_FILTER = DisabledOpacityFilter()
    widget.installEventFilter(_OPACITY_FILTER)
    _apply_disabled_opacity(widget)
    return widget


class IconComboBox(QComboBox):
    """Combo box with an explicit chevron and a popup anchored below the control."""

    def __init__(self, icon_family: str | None, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon_family = icon_family
        self._chevron = QToolButton(self)
        self._chevron.setObjectName("comboChevron")
        self._chevron.setAutoRaise(True)
        self._chevron.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._chevron.clicked.connect(self.showPopup)
        self._set_chevron(False)

    def _set_chevron(self, opened: bool) -> None:
        if self._icon_family:
            font = QFont(self._icon_family)
            font.setPixelSize(13)
            self._chevron.setFont(font)
            self._chevron.setText("\uf077" if opened else "\uf078")
        else:
            self._chevron.setText("⌃" if opened else "⌄")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._chevron.setGeometry(self.width() - 28, 0, 26, self.height())

    def showPopup(self) -> None:
        super().showPopup()
        self._set_chevron(True)
        self._position_popup_below()
        QTimer.singleShot(0, self._position_popup_below)

    def hidePopup(self) -> None:
        super().hidePopup()
        self._set_chevron(False)

    def _position_popup_below(self) -> None:
        popup = self.view().window()
        if popup.isVisible():
            popup.move(self.mapToGlobal(QPoint(0, self.height())))


class RowClickFilter(QObject):
    """Selects the parent table row when clicking an embedded cell widget."""

    def __init__(self, table: QTableWidget, widget: QWidget):
        super().__init__(widget)
        self._table = table
        self._widget = widget

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            pos = self._widget.mapTo(self._table.viewport(), self._widget.rect().center())
            index = self._table.indexAt(pos)
            if index.isValid():
                mods = QApplication.keyboardModifiers()
                if not (mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
                    if index.row() not in {i.row() for i in self._table.selectionModel().selectedRows()}:
                        self._table.selectRow(index.row())
        return False


def load_icon_font() -> str | None:
    """Register the bundled Font Awesome Free Solid font; return its family name."""
    candidates = [p for p in sorted(FONT_DIR.glob("*")) if p.suffix.lower() in (".otf", ".ttf")
                  and "brands" not in p.name.lower()]
    for path in candidates:
        font_id = QFontDatabase.addApplicationFont(str(path))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id != -1 else []
        if families:
            return families[0]
    return None


def apply_icon(button: QPushButton, family: str | None, name: str, pixel: int = 16) -> None:
    glyph, fallback = ICONS[name]
    if family:
        font = QFont(family)
        font.setPixelSize(pixel)
        button.setFont(font)
        button.setText(glyph)
    else:
        button.setText(fallback)


def icon_button(family: str | None, icon: str, tooltip: str, role: str = "",
                size: tuple[int, int] = (46, CONTROL_HEIGHT)) -> QPushButton:
    button = QPushButton()
    button.setObjectName(role)
    button.setToolTip(tooltip)
    button.setFixedSize(size[0], CONTROL_HEIGHT)
    button.setProperty("iconOnly", True)
    apply_icon(button, family, icon)
    return track_disabled_opacity(button)


class RuleSplitterHandle(QSplitterHandle):
    """Vertical splitter handle styled as a 1px rule with an easy-to-grab hit area."""

    def __init__(self, orientation: Qt.Orientation, parent: QSplitter):
        super().__init__(orientation, parent)
        self._hovered = False
        self.setCursor(Qt.CursorShape.SplitVCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        y = self.height() // 2
        color = QColor("#606D87" if self._hovered else "#4c566a")
        painter.fillRect(0, y, self.width(), 1, color)


class OptionsSplitter(QSplitter):
    """Vertical splitter whose handle matches the section separator line."""

    def createHandle(self) -> QSplitterHandle:
        return RuleSplitterHandle(self.orientation(), self)


class ProgressDelegate(QStyledItemDelegate):
    """Paints a blue progress bar inside the Progress column (expects 0.0..1.0 ratio)."""

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)
        progress = index.data(Qt.ItemDataRole.UserRole)
        if progress is None:
            return
        ratio = max(0.0, min(1.0, float(progress)))
        bar: QRect = option.rect.adjusted(6, 4, -6, -4)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2e3440"))
        painter.drawRoundedRect(bar, 3, 3)
        fill = int(bar.width() * ratio)
        if fill > 0:
            painter.setBrush(QColor("#2563d9"))
            painter.drawRoundedRect(QRect(bar.x(), bar.y(), fill, bar.height()), 3, 3)
        painter.setPen(QPen(QColor("#eceff4")))
        painter.drawText(bar, Qt.AlignmentFlag.AlignCenter, f"{int(ratio * 100)}%")
        painter.restore()


class ResizableTable(QTableWidget):
    """Table with interactive column dividers that can expand beyond the viewport with scrolling."""

    columns_changed = Signal(list)
    layout_changed = Signal()

    def __init__(
        self,
        headers: list[str],
        default_widths: list[int],
        *,
        has_saved_widths: bool = False,
        flex_column: int = 1,
        parent: QWidget | None = None,
    ):
        columns = len(headers)
        super().__init__(0, columns, parent)
        self._has_saved_widths = has_saved_widths
        self._flex_column = max(0, min(columns - 1, flex_column))
        self._initial_fitted = False
        self._syncing_columns = False

        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)

        header = self.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(False)
        header.setSectionsMovable(True)
        header.setHighlightSections(False)
        header.setMinimumSectionSize(48)

        self._syncing_columns = True
        try:
            for c in range(columns):
                w = default_widths[c] if c < len(default_widths) else 100
                self.setColumnWidth(c, max(header.minimumSectionSize(), int(round(w))))
        finally:
            self._syncing_columns = False

        header.sectionResized.connect(self._on_section_resized)
        header.sectionMoved.connect(lambda *_: self.layout_changed.emit())
        header.sortIndicatorChanged.connect(lambda *_: self.layout_changed.emit())


    def has_custom_widths(self) -> bool:
        return self._has_saved_widths

    def current_widths(self) -> list[int]:
        return [self.columnWidth(c) for c in range(self.columnCount())]

    def fit_initial_columns(self) -> None:
        """Fit columns to 100% of the initial viewport width when no saved widths exist."""
        if self._initial_fitted or self._has_saved_widths:
            self._initial_fitted = True
            return
        avail = self.viewport().width()
        cols = self.columnCount()
        if avail <= 0 or cols <= 0:
            return
        self._initial_fitted = True
        min_w = self.horizontalHeader().minimumSectionSize()
        widths = [max(min_w, self.columnWidth(c)) for c in range(cols)]
        total = sum(widths)
        if total < avail:
            widths[self._flex_column] += avail - total
        elif total > avail:
            scale = avail / total
            widths = [max(min_w, int(round(w * scale))) for w in widths]
            diff = avail - sum(widths)
            if diff != 0:
                order = [self._flex_column] + [c for c in range(cols - 1, -1, -1) if c != self._flex_column]
                step = 1 if diff > 0 else -1
                while diff != 0:
                    moved = False
                    for c in order:
                        if 0 <= c < cols and widths[c] + step >= min_w:
                            widths[c] += step
                            diff -= step
                            moved = True
                            if diff == 0:
                                break
                    if not moved:
                        break

        self._syncing_columns = True
        try:
            for c, w in enumerate(widths):
                self.setColumnWidth(c, w)
        finally:
            self._syncing_columns = False

    def _on_section_resized(self, _index: int, _old_size: int, _new_size: int) -> None:
        if self._syncing_columns:
            return
        self._has_saved_widths = True
        self.columns_changed.emit(self.current_widths())


class SortableTableWidgetItem(QTableWidgetItem):
    """Compare numeric sort values when available and use text otherwise."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(SORT_ROLE)
        right = other.data(SORT_ROLE)
        if left is not None and right is not None:
            return left < right
        return self.text().casefold() < other.text().casefold()


class CaptureTableWidget(QWidget):
    """Single-row read-only selectable table for captured session values + Copy / Save buttons."""

    save_token_requested = Signal(str, str)  # target_key, token_value

    def __init__(self, field: Field, parent: QWidget | None = None):
        super().__init__(parent)
        self._field = field
        self._values: dict[str, str] = {}
        self._edits: dict[str, QLineEdit] = {}
        self.setFixedWidth(SERVER_FIELD_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(6)

        cols = field.table_columns
        self.table = QTableWidget(1, len(cols), self)
        self.table.setFixedWidth(SERVER_FIELD_WIDTH)
        self.table.setHorizontalHeaderLabels([c.caption for c in cols])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setStyleSheet(
            "QTableWidget { background: #20242C; border: 1px solid #3b4252; border-radius: 6px; }"
            "QHeaderView { background-color: #20242C; border: none; border-bottom: 1px solid #3b4252; }"
            "QHeaderView::section { background-color: #20242C; border: none;"
            " border-right: 1px solid #2e3440; border-bottom: 1px solid #3b4252;"
            " padding: 5px 10px; text-align: left; }"
        )

        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        header.setSectionsMovable(False)
        header.setHighlightSections(False)
        header.setMinimumSectionSize(60)

        for idx, col in enumerate(cols):
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(0, idx, item)

            edit = QLineEdit()
            edit.setReadOnly(True)
            edit.setFrame(False)
            edit.setStyleSheet(
                "QLineEdit { background: transparent; border: none; border-radius: 0px;"
                " padding: 0px 8px; color: #eceff4;"
                " selection-background-color: #2563d9; selection-color: #ffffff; }"
            )
            self._edits[col.source] = edit
            self.table.setCellWidget(0, idx, edit)

        header_h = header.sizeHint().height()
        self.table.setFixedHeight(header_h + 26 + 2)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)
        compact_btn_style = (
            "QPushButton { background: #20242C; border: none; outline: none;"
            " border-radius: 8px; padding: 6px 10px; }"
            "QPushButton:hover:!disabled { background: #606D87; }"
        )

        self.copy_btn: QPushButton | None = None
        self.save_txt_btn: QPushButton | None = None
        if field.export is not None:
            self.copy_btn = QPushButton("Copy")
            self.copy_btn.setFixedHeight(CONTROL_HEIGHT)
            self.copy_btn.setStyleSheet(compact_btn_style)
            self.copy_btn.clicked.connect(self._copy_text)
            track_disabled_opacity(self.copy_btn)
            btn_row.addWidget(self.copy_btn)

            self.save_txt_btn = QPushButton("Save as txt")
            self.save_txt_btn.setFixedHeight(CONTROL_HEIGHT)
            self.save_txt_btn.setStyleSheet(compact_btn_style)
            self.save_txt_btn.clicked.connect(self._save_txt)
            track_disabled_opacity(self.save_txt_btn)
            btn_row.addWidget(self.save_txt_btn)

        self.save_token_btn: QPushButton | None = None
        if field.save_token is not None:
            self.save_token_btn = QPushButton(field.save_token.caption)
            self.save_token_btn.setFixedHeight(CONTROL_HEIGHT)
            self.save_token_btn.setStyleSheet(compact_btn_style)
            self.save_token_btn.clicked.connect(self._emit_save_token)
            track_disabled_opacity(self.save_token_btn)
            btn_row.addWidget(self.save_token_btn)

        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        self._sync_buttons()

    def set_captured_values(self, values: dict[str, str]) -> None:
        self._values = dict(values)
        for source, edit in self._edits.items():
            val = self._values.get(source, "")
            if edit.text() != val:
                edit.setText(val)
                edit.setCursorPosition(0)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_any = any(bool(self._values.get(c.source, "").strip()) for c in self._field.table_columns)
        if self.copy_btn is not None:
            self.copy_btn.setEnabled(has_any)
        if self.save_txt_btn is not None:
            self.save_txt_btn.setEnabled(has_any)
        if self.save_token_btn is not None and self._field.save_token is not None:
            tok = self._values.get(self._field.save_token.source, "").strip()
            self.save_token_btn.setEnabled(bool(tok))

    def _rendered_export(self) -> str:
        if self._field.export is None:
            return ""
        ctx = {c.source: self._values.get(c.source, "") for c in self._field.table_columns}
        ctx.update(self._values)
        return render(self._field.export.template, ctx)

    def _copy_text(self) -> None:
        text = self._rendered_export()
        if text:
            QGuiApplication.clipboard().setText(text)

    def _save_txt(self) -> None:
        if self._field.export is None:
            return
        text = self._rendered_export()
        if not text:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Session Tokens",
            self._field.export.default_filename,
            "Text files (*.txt)",
        )
        if path:
            Path(path).write_text(text + "\n", encoding="utf-8")

    def _emit_save_token(self) -> None:
        if self._field.save_token is None:
            return
        tok = self._values.get(self._field.save_token.source, "").strip()
        if tok:
            self.save_token_requested.emit(self._field.save_token.to_key, tok)


class AddServerDialog(QDialog):
    """Modal for adding a new server either via a remote URL or a local file (JSON or YAML)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Server")
        self.setMinimumWidth(480)
        self.added_server: Server | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Add Server from URL"))
        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setFixedHeight(CONTROL_HEIGHT)
        self.url_input.setPlaceholderText("https://example.com/server.json (or .yaml)")
        self.url_input.returnPressed.connect(self._add_from_url)
        track_disabled_opacity(self.url_input)

        self.add_url_btn = QPushButton("Add URL")
        self.add_url_btn.setFixedHeight(CONTROL_HEIGHT)
        self.add_url_btn.clicked.connect(self._add_from_url)
        track_disabled_opacity(self.add_url_btn)

        url_row.addWidget(self.url_input, 1)
        url_row.addWidget(self.add_url_btn)
        layout.addLayout(url_row)

        sep = QFrame()
        sep.setObjectName("rule")
        layout.addWidget(sep)

        layout.addWidget(QLabel("Or Import a Local File (JSON or YAML)"))
        bottom = QHBoxLayout()
        self.browse_btn = QPushButton("Browse For File")
        self.browse_btn.setFixedHeight(CONTROL_HEIGHT)
        self.browse_btn.clicked.connect(self._browse_file)
        track_disabled_opacity(self.browse_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setFixedHeight(CONTROL_HEIGHT)
        self.close_btn.clicked.connect(self.reject)
        track_disabled_opacity(self.close_btn)

        bottom.addWidget(self.browse_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

    def _set_busy(self, busy: bool) -> None:
        self.url_input.setEnabled(not busy)
        self.add_url_btn.setEnabled(not busy)
        self.browse_btn.setEnabled(not busy)
        self.close_btn.setEnabled(not busy)
        QApplication.processEvents()

    def _add_from_url(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Add Server", "Enter a URL to a server definition (.json or .yaml).")
            return
        self._set_busy(True)
        try:
            server = add_url_server(url)
        except ServerDefError as exc:
            self._set_busy(False)
            QMessageBox.warning(self, "Invalid Server Definition", str(exc))
            return
        self._set_busy(False)
        self.added_server = server
        self.accept()

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Browse For Server Definition",
            "",
            "Server definitions (*.json *.yaml *.yml);;All files (*)",
        )
        if not path:
            return
        try:
            server = add_local_server(Path(path))
        except (ServerDefError, OSError) as exc:
            QMessageBox.warning(self, "Invalid Server Definition", str(exc))
            return
        self.added_server = server
        self.accept()


class ServersDialog(QDialog):
    """Modal table listing all configured servers with Delete, Add, and Close actions."""

    def __init__(self, parent: QWidget | None, loaded: LoadResult | None):
        super().__init__(parent)
        self.setWindowTitle("Servers")
        self.setMinimumSize(720, 380)
        self._loaded = loaded
        self.servers_changed = False
        self._locations: list[str] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.table = ResizableTable(
            ["Name", "Location", "Base URL"],
            SERVERS_COLUMN_WIDTHS,
            has_saved_widths=False,
            flex_column=1,
            parent=self,
        )
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("remove")
        self.delete_btn.setFixedHeight(CONTROL_HEIGHT)
        self.delete_btn.clicked.connect(self._delete_selected)
        track_disabled_opacity(self.delete_btn)

        self.add_btn = QPushButton("Add")
        self.add_btn.setFixedHeight(CONTROL_HEIGHT)
        self.add_btn.clicked.connect(self._open_add_modal)
        track_disabled_opacity(self.add_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setFixedHeight(CONTROL_HEIGHT)
        self.close_btn.clicked.connect(self.accept)
        track_disabled_opacity(self.close_btn)

        bottom.addWidget(self.delete_btn)
        bottom.addWidget(self.add_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

        self._populate()
        QTimer.singleShot(0, self.table.fit_initial_columns)

    def _populate(self) -> None:
        self._locations.clear()
        self.table.setRowCount(0)
        entries = list_server_entries(self._loaded)
        for row, entry in enumerate(entries):
            self.table.insertRow(row)
            self._locations.append(entry.location)

            name_item = QTableWidgetItem(entry.name)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if entry.error:
                name_item.setForeground(QColor("#ef4444"))
                name_item.setToolTip(entry.error)
            self.table.setItem(row, 0, name_item)

            loc_item = QTableWidgetItem(entry.location)
            loc_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            loc_item.setToolTip(entry.location)
            self.table.setItem(row, 1, loc_item)

            base_item = QTableWidgetItem(entry.base_url)
            base_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            base_item.setToolTip(entry.base_url)
            self.table.setItem(row, 2, base_item)

        self._sync_buttons()

    def _sync_buttons(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        has_sel = bool(rows)
        can_delete = has_sel and all(self._locations[idx.row()] != "Embedded" for idx in rows)
        self.delete_btn.setEnabled(can_delete)
        if has_sel and not can_delete:
            self.delete_btn.setToolTip("Embedded server definitions cannot be deleted here")
        else:
            self.delete_btn.setToolTip("")

    def _delete_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return
        for row in rows:
            if 0 <= row < len(self._locations):
                try:
                    remove_server(self._locations[row])
                except OSError as exc:
                    QMessageBox.warning(self, "Delete Server", f"Could not remove server: {exc}")
        self.servers_changed = True
        self._loaded = load_servers(include_urls=False)
        self._populate()

    def _open_add_modal(self) -> None:
        dialog = AddServerDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.added_server is not None:
            self.servers_changed = True
            self._loaded = load_servers(include_urls=False)
            self._populate()


class KeysDialog(QDialog):
    """Modal table of saved API keys, with show/hide, copy, row selection, and delete."""

    def __init__(self, parent, servers: dict[str, Server], icon_family: str | None):
        super().__init__(parent)
        self.setWindowTitle("API Keys")
        self.setMinimumSize(680, 360)
        self._servers = servers
        self._family = icon_family
        self._entries = saved_keys()
        self.keys_changed = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.table = ResizableTable(
            ["Server", "Key"],
            KEYS_COLUMN_WIDTHS,
            has_saved_widths=False,
            flex_column=1,
            parent=self,
        )
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.itemSelectionChanged.connect(self._sync_delete_button)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("remove")
        self.delete_btn.setFixedHeight(CONTROL_HEIGHT)
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._delete_selected)
        track_disabled_opacity(self.delete_btn)

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(CONTROL_HEIGHT)
        close_btn.clicked.connect(self.accept)
        track_disabled_opacity(close_btn)

        bottom.addWidget(self.delete_btn)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        self._populate()
        QTimer.singleShot(0, self.table.fit_initial_columns)

    def _populate(self) -> None:
        self.table.setRowCount(0)
        for row, entry in enumerate(self._entries):
            self.table.insertRow(row)
            server = self._servers.get(entry.server_id)
            label = server.name if server else entry.server_id
            if server and len(server.fields) > 1 and (f := server.field(entry.field_key)):
                label = f"{label} ({f.caption})"

            server_item = QTableWidgetItem(label)
            server_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 0, server_item)

            key_item = QTableWidgetItem()
            key_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 1, key_item)

            cell = QWidget()
            cell.setStyleSheet("background: transparent;")
            box = QHBoxLayout(cell)
            box.setContentsMargins(6, 2, 6, 2)
            box.setSpacing(6)
            edit = QLineEdit(entry.value)
            edit.setFixedHeight(CONTROL_HEIGHT)
            edit.setReadOnly(True)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            show = icon_button(self._family, "eye", "Show", size=(38, CONTROL_HEIGHT))
            copy = icon_button(self._family, "copy", "Copy", size=(38, CONTROL_HEIGHT))
            show.clicked.connect(lambda _=False, e=edit, b=show: self._toggle(e, b))
            copy.clicked.connect(lambda _=False, v=entry.value: QGuiApplication.clipboard().setText(v))
            box.addWidget(edit, 1)
            box.addWidget(show)
            box.addWidget(copy)
            self.table.setCellWidget(row, 1, cell)

        self._sync_delete_button()

    def _sync_delete_button(self) -> None:
        has_sel = bool(self.table.selectionModel() and self.table.selectionModel().selectedRows())
        self.delete_btn.setEnabled(has_sel)

    def _delete_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return
        for row in rows:
            entry = self._entries[row]
            delete_key(entry.server_id, entry.field_key)
            del self._entries[row]
        self.keys_changed = True
        self._populate()

    def _toggle(self, edit: QLineEdit, button: QPushButton) -> None:
        hidden = edit.echoMode() == QLineEdit.EchoMode.Password
        edit.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        apply_icon(button, self._family, "eye_slash" if hidden else "eye")
        button.setToolTip("Hide" if hidden else "Show")


class MainWindow(QMainWindow):
    job_event = Signal(object)
    choices_ready = Signal(str, str, int, object, str)  # server id, field key, generation, items, error
    servers_ready = Signal(int, object)  # generation, LoadResult
    button_action_done = Signal(str, str, object, str, str)  # server_id, field_key, captured, message, error

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DrainPype File Uploader")
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        self.setAcceptDrops(True)

        state = _load_ui_state()
        win_w = max(MIN_WINDOW_SIZE[0], int(state.get("window_width", DEFAULT_WINDOW_SIZE[0])))
        win_h = max(MIN_WINDOW_SIZE[1], int(state.get("window_height", DEFAULT_WINDOW_SIZE[1])))
        self._is_maximized: bool = bool(state.get("window_maximized", False))
        if self._is_maximized and self._looks_screen_sized(win_w, win_h):
            win_w, win_h = DEFAULT_WINDOW_SIZE
        self._normal_size: tuple[int, int] = (win_w, win_h)
        self._pending_normal_size: tuple[int, int] = (win_w, win_h)
        self._last_max_size: tuple[int, int] | None = None
        self._state_changed_at: float = 0.0
        self._options_height: int = max(MIN_OPTIONS_HEIGHT, int(state.get("options_height", DEFAULT_OPTIONS_HEIGHT)))

        saved_cols = state.get("table_columns")
        self._has_saved_cols = (isinstance(saved_cols, list) and len(saved_cols) == len(COLUMNS)
                                and all(isinstance(x, (int, float)) and x > 0 for x in saved_cols))
        self._initial_col_widths = [int(round(x)) for x in saved_cols] if self._has_saved_cols else list(COLUMN_WIDTHS)
        saved_order = state.get("table_column_order")
        valid_order = (
            isinstance(saved_order, list)
            and len(saved_order) == len(COLUMNS)
            and all(isinstance(x, int) for x in saved_order)
            and sorted(saved_order) == list(range(len(COLUMNS)))
        )
        self._initial_column_order = list(saved_order) if valid_order else list(range(len(COLUMNS)))
        try:
            saved_sort_column = int(state.get("table_sort_column", C_NUMBER))
        except (TypeError, ValueError):
            saved_sort_column = C_NUMBER
        self._initial_sort_column = (
            saved_sort_column if 0 <= saved_sort_column < len(COLUMNS) else C_NUMBER
        )
        self._initial_sort_order = (
            Qt.SortOrder.DescendingOrder if state.get("table_sort_descending") else Qt.SortOrder.AscendingOrder
        )

        raw_last_dir = state.get("last_files_dir")
        self._last_files_dir: str = raw_last_dir if isinstance(raw_last_dir, str) else ""

        saved_max_uploads = state.get("max_uploads", DEFAULT_MAX_UPLOADS)
        try:
            self._max_uploads = max(MIN_MAX_UPLOADS, min(MAX_MAX_UPLOADS, int(saved_max_uploads)))
        except (TypeError, ValueError):
            self._max_uploads = DEFAULT_MAX_UPLOADS

        self._shown_once = False
        self._ui_ready = False
        self._resize_save_timer = QTimer(self)
        self._resize_save_timer.setSingleShot(True)
        self._resize_save_timer.setInterval(300)
        self._resize_save_timer.timeout.connect(self._save_window_size)

        self._layout_save_timer = QTimer(self)
        self._layout_save_timer.setSingleShot(True)
        self._layout_save_timer.setInterval(250)
        self._layout_save_timer.timeout.connect(self._save_layout_state)

        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(250)
        self._eta_timer.timeout.connect(self._refresh_active_etas)

        self.resize(win_w, win_h)

        win_icon = _resolve_window_icon()
        if win_icon is not None:
            self.setWindowIcon(win_icon)

        self._icon_family = load_icon_font()
        self._servers: dict[str, Server] = {}
        self._last_load_result: LoadResult | None = None
        self._servers_gen = 0
        self._inputs: dict[str, QWidget] = {}
        self._save_boxes: dict[str, QCheckBox] = {}
        self._saved_key_values: dict[str, str] = {}
        self._user_checked: dict[str, bool] = {}
        self._captured_values: dict[str, dict[str, str]] = {}
        self._choice_gen: dict[str, int] = {}
        self._row_of: dict[int, int] = {}
        self._job_states: dict[int, State] = {}
        self._removed_ids: set[int] = set()

        self.uploader = Uploader(listener=self.job_event.emit)
        self.uploader.set_max_uploads(self._max_uploads)
        self.job_event.connect(self._on_job)
        self.choices_ready.connect(self._on_choices)
        self.servers_ready.connect(self._on_servers_ready)
        self.button_action_done.connect(self._on_button_action_done)

        self._build_ui()
        self._reload_servers(initial=True)
        self._sync_controls()

    def starts_maximized(self) -> bool:
        return self._is_maximized

    @staticmethod
    def _looks_screen_sized(width: int, height: int) -> bool:
        app = QApplication.instance()
        if app is None:
            return False
        for screen in app.screens():
            avail = screen.availableGeometry()
            full = screen.geometry()
            if (width >= avail.width() - 16 and height >= avail.height() - 40) or (
                width >= full.width() - 16 and height >= full.height() - 40
            ):
                return True
        return False

    def _is_window_maximized(self) -> bool:
        return bool(
            self._is_maximized
            or self.isMaximized()
            or self.isFullScreen()
            or (self.windowState() & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen))
        )

    def _save_window_size(self) -> None:
        if not self._ui_ready or self._is_window_maximized() or self.isMinimized():
            return
        w, h = self._pending_normal_size
        if w >= MIN_WINDOW_SIZE[0] and h >= MIN_WINDOW_SIZE[1] and not self._looks_screen_sized(w, h):
            self._normal_size = (w, h)
            _save_ui_state({
                "window_width": w,
                "window_height": h,
                "window_maximized": False,
            })

    def _restore_normal_size_if_needed(self) -> None:
        if self._is_window_maximized() or self.isMinimized():
            return
        cur = (self.width(), self.height())
        if cur == self._last_max_size or self._looks_screen_sized(*cur):
            self.resize(*self._normal_size)

    # ------------------------------------------------------------------ persistence
    def _schedule_save_layout_state(self) -> None:
        if self._ui_ready:
            self._layout_save_timer.start()

    def _save_layout_state(self) -> None:
        if self._layout_save_timer.isActive():
            self._layout_save_timer.stop()
        payload: dict[str, Any] = {
            "options_height": self._options_height,
        }
        if self.table.has_custom_widths():
            payload["table_columns"] = self.table.current_widths()
        header = self.table.horizontalHeader()
        payload["table_column_order"] = [header.logicalIndex(i) for i in range(header.count())]
        payload["table_sort_column"] = header.sortIndicatorSection()
        payload["table_sort_descending"] = (
            header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        )
        _save_ui_state(payload)

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if not self._ui_ready:
            return
        sizes = self.splitter.sizes()
        if sizes:
            self._options_height = max(MIN_OPTIONS_HEIGHT, sizes[0])
            self._schedule_save_layout_state()

    def _on_table_columns_changed(self, _widths: list[int]) -> None:
        self._schedule_save_layout_state()

    def _on_table_layout_changed(self) -> None:
        self._schedule_save_layout_state()
        QTimer.singleShot(0, self._reindex_upload_rows)

    def _reindex_upload_rows(self) -> None:
        if not hasattr(self, "table"):
            return
        self._row_of = {
            self.table.item(row, C_NAME).data(Qt.ItemDataRole.UserRole): row
            for row in range(self.table.rowCount())
            if self.table.item(row, C_NAME) is not None
        }

    def _sort_and_reindex_upload_rows(self) -> None:
        header = self.table.horizontalHeader()
        if self.table.isSortingEnabled():
            self.table.sortItems(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self._reindex_upload_rows()

    def _renumber_upload_rows(self) -> None:
        for number, job in enumerate(self.uploader.jobs, start=1):
            row = self._row_of.get(job.id)
            if row is None:
                continue
            item = self.table.item(row, C_NUMBER)
            if item is not None:
                item.setText(str(number))
                item.setData(SORT_ROLE, number)

    # ------------------------------------------------------------------ building
    def _rule(self) -> QFrame:
        line = QFrame()
        line.setObjectName("rule")
        return line

    def _icon_button(self, icon: str, tooltip: str, role: str = "") -> QPushButton:
        return icon_button(self._icon_family, icon, tooltip, role)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(8)

        root.addWidget(QLabel("Select Server"))
        top = QHBoxLayout()
        self.server_box = IconComboBox(self._icon_family)
        self.server_box.setMinimumWidth(165)
        self.server_box.setFixedHeight(CONTROL_HEIGHT)
        self.server_box.currentIndexChanged.connect(self._rebuild_form)
        track_disabled_opacity(self.server_box)

        self.servers_btn = QPushButton("Servers")
        self.servers_btn.setFixedHeight(CONTROL_HEIGHT)
        self.servers_btn.setToolTip("Manage local and remote servers")
        self.servers_btn.clicked.connect(self._open_servers_modal)
        track_disabled_opacity(self.servers_btn)

        self.reload_btn = self._icon_button("refresh", "Reload servers")
        self.reload_btn.clicked.connect(lambda: self._reload_servers())

        self.keys_btn = QPushButton("API Keys")
        self.keys_btn.setFixedSize(147, CONTROL_HEIGHT)
        self.keys_btn.clicked.connect(self._api_keys)
        track_disabled_opacity(self.keys_btn)

        for widget in (self.server_box, self.servers_btn, self.reload_btn):
            top.addWidget(widget)
        top.addStretch(1)
        top.addWidget(self.keys_btn)
        root.addLayout(top)
        root.addWidget(self._rule())

        options_header = QHBoxLayout()
        options_header.setContentsMargins(0, 0, 0, 0)
        section = QLabel("Server Options")
        section.setObjectName("section")
        options_header.addWidget(section)
        options_header.addStretch(1)

        self.open_base_url_btn = QPushButton("Open Website")
        self.open_base_url_btn.setFixedHeight(CONTROL_HEIGHT)
        self.open_base_url_btn.setStyleSheet(
            "QPushButton { background: #20242C; border: none; outline: none;"
            " border-radius: 8px; padding: 5px 12px; font-size: 12px; }"
            "QPushButton:hover:!disabled { background: #606D87; }"
        )
        self.open_base_url_btn.clicked.connect(self._open_base_url)
        track_disabled_opacity(self.open_base_url_btn)
        options_header.addWidget(self.open_base_url_btn)
        root.addLayout(options_header)

        self.form_host = QHBoxLayout()
        self.form_host.setSpacing(26)
        self.form_host.setContentsMargins(18, 2, 10, 4)
        form_holder = QWidget()
        form_holder.setLayout(self.form_host)

        self.options_scroll = QScrollArea()
        self.options_scroll.setObjectName("options_scroll")
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.options_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.options_scroll.setMinimumHeight(MIN_OPTIONS_HEIGHT)
        self.options_scroll.setWidget(form_holder)

        lower_pane = QWidget()
        self.lower_pane = lower_pane
        lower_layout = QVBoxLayout(lower_pane)
        lower_layout.setContentsMargins(0, 8, 0, 0)
        lower_layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 4)
        bar.setSpacing(6)
        self.add_btn = self._icon_button("plus", "Add files", "add")
        self.add_btn.clicked.connect(self._add_files)
        self.remove_btn = self._icon_button("trash", "Remove selected", "remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.start_btn = self._icon_button("play", "Start selected uploads", "start")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = self._icon_button("stop", "Stop selected uploads", "stop")
        self.stop_btn.clicked.connect(self._stop)
        self.move_up_btn = self._icon_button("move_up", "Move selected rows up", "move_queue")
        self.move_up_btn.clicked.connect(lambda: self._move_selected_rows(-1))
        self.move_down_btn = self._icon_button("move_down", "Move selected rows down", "move_queue")
        self.move_down_btn.clicked.connect(lambda: self._move_selected_rows(1))
        self.restart_btn = self._icon_button("restart", "Upload again", "restart")
        self.restart_btn.clicked.connect(self._restart)

        max_label = QLabel("Max No. of uploads")
        self.max_uploads_spin = QSpinBox()
        self.max_uploads_spin.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.max_uploads_spin.setRange(MIN_MAX_UPLOADS, MAX_MAX_UPLOADS)
        self.max_uploads_spin.setValue(self._max_uploads)
        self.max_uploads_spin.lineEdit().setTextMargins(6, 0, 0, 0)
        self.max_uploads_spin.setFixedSize(74, CONTROL_HEIGHT)
        self.max_uploads_spin.setToolTip("Maximum concurrent uploads (1–100)")
        self.max_uploads_spin.valueChanged.connect(self._on_max_uploads_changed)
        track_disabled_opacity(self.max_uploads_spin)
        self.max_uploads_apply_btn = QPushButton("Apply")
        self.max_uploads_apply_btn.setFixedSize(64, CONTROL_HEIGHT)
        self.max_uploads_apply_btn.setToolTip("Apply the selected maximum to the upload queue")
        self.max_uploads_apply_btn.clicked.connect(self._apply_max_uploads)
        self.max_uploads_apply_btn.setEnabled(False)
        track_disabled_opacity(self.max_uploads_apply_btn)

        bar.addWidget(self.add_btn)
        bar.addWidget(self.remove_btn)
        bar.addSpacing(22)
        bar.addWidget(self.start_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.restart_btn)
        bar.addSpacing(22)
        bar.addWidget(self.move_up_btn)
        bar.addWidget(self.move_down_btn)
        bar.addSpacing(22)
        bar.addWidget(max_label)
        bar.addWidget(self.max_uploads_spin)
        bar.addWidget(self.max_uploads_apply_btn)
        bar.addStretch(1)
        lower_layout.addLayout(bar)

        self.table = self._build_table()
        lower_layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.copy_btn = QPushButton("Copy URLs")
        self.copy_btn.setFixedSize(147, CONTROL_HEIGHT)
        self.copy_btn.clicked.connect(self._copy_urls)
        track_disabled_opacity(self.copy_btn)
        self.save_btn = QPushButton("Save URLs")
        self.save_btn.setFixedSize(147, CONTROL_HEIGHT)
        self.save_btn.clicked.connect(self._save_urls)
        track_disabled_opacity(self.save_btn)
        self.stats = QLabel("Total Files: 0 | 0 Completed | 0 Failed")
        bottom.addWidget(self.copy_btn)
        bottom.addWidget(self.save_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.stats)
        lower_layout.addLayout(bottom)

        self.splitter = OptionsSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(7)
        self.splitter.addWidget(self.options_scroll)
        self.splitter.addWidget(lower_pane)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        root.addWidget(self.splitter, 1)

        footer = QHBoxLayout()
        footer.addWidget(QLabel(
            f'Github source: ' + '<a href="{REPO_URL}" style="color:#4da3ff;text-decoration:underline;">DogFoxX/drainpype</a>'
        ))
        footer.itemAt(0).widget().setOpenExternalLinks(True)
        footer.addStretch(1)
        root.addLayout(footer)
        self.setCentralWidget(central)

    def _build_table(self) -> ResizableTable:
        table = ResizableTable(
            COLUMNS,
            self._initial_col_widths,
            has_saved_widths=self._has_saved_cols,
            flex_column=C_NAME,
            parent=self,
        )
        table.setItemDelegateForColumn(C_PROGRESS, ProgressDelegate(table))
        table.selectionModel().selectionChanged.connect(self._sync_needs)
        table.columns_changed.connect(self._on_table_columns_changed)
        table.layout_changed.connect(self._on_table_layout_changed)
        header = table.horizontalHeader()
        header.setSortIndicatorShown(True)
        for target_visual, logical in enumerate(self._initial_column_order):
            current_visual = header.visualIndex(logical)
            if current_visual != target_visual:
                header.moveSection(current_visual, target_visual)
        table.setSortingEnabled(True)
        table.sortItems(self._initial_sort_column, self._initial_sort_order)
        header_h = table.horizontalHeader().sizeHint().height()
        min_table_height = (
            header_h
            + MIN_VISIBLE_ROWS * ROW_HEIGHT
            + 2 * table.frameWidth()
            + table.horizontalScrollBar().sizeHint().height()
            + 2
        )
        table.setMinimumHeight(min_table_height)
        return table

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            QTimer.singleShot(0, self._finalize_initial_layout)

    def _finalize_initial_layout(self) -> None:
        total_h = self.splitter.height()
        handle_h = self.splitter.handleWidth()
        min_lower_h = max(160, self.lower_pane.minimumSizeHint().height())
        if total_h > 0:
            max_opt_h = max(MIN_OPTIONS_HEIGHT, total_h - handle_h - min_lower_h)
            opt_h = max(MIN_OPTIONS_HEIGHT, min(self._options_height, max_opt_h))
            self._options_height = opt_h
            self.splitter.setSizes([opt_h, max(min_lower_h, total_h - handle_h - opt_h)])
        self.table.fit_initial_columns()
        if not self._is_window_maximized() and not self._looks_screen_sized(self.width(), self.height()):
            self._normal_size = (self.width(), self.height())
            self._pending_normal_size = self._normal_size
        else:
            self._last_max_size = (self.width(), self.height())
        self._ui_ready = True

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._ui_ready:
            return
        sizes = self.splitter.sizes()
        if sizes:
            self._options_height = max(MIN_OPTIONS_HEIGHT, sizes[0])
        if self._is_window_maximized() or self.isMinimized():
            if self._resize_save_timer.isActive():
                self._resize_save_timer.stop()
            if self._is_window_maximized():
                self._last_max_size = (self.width(), self.height())
            return

        w, h = self.width(), self.height()
        if self._looks_screen_sized(w, h) or time.monotonic() - self._state_changed_at < 0.35:
            if self._resize_save_timer.isActive():
                self._resize_save_timer.stop()
            return

        self._pending_normal_size = (w, h)
        self._resize_save_timer.start()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            was_maximized = self._is_maximized
            now_maximized = bool(
                self.windowState() & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen)
            )
            self._is_maximized = now_maximized
            self._state_changed_at = time.monotonic()
            if self._resize_save_timer.isActive():
                self._resize_save_timer.stop()
            if now_maximized:
                self._last_max_size = (self.width(), self.height())
                if self._ui_ready:
                    _save_ui_state({"window_maximized": True})
            elif was_maximized:
                if self._ui_ready:
                    _save_ui_state({"window_maximized": False})
                QTimer.singleShot(0, self._restore_normal_size_if_needed)

    # ------------------------------------------------------------------ servers + form
    def _current_server(self) -> Server | None:
        return self._servers.get(self.server_box.currentData())

    def _apply_server_map(self, servers: dict[str, Server]) -> None:
        previous = self.server_box.currentData()
        self._servers = servers
        self.server_box.blockSignals(True)
        self.server_box.clear()
        for server in self._servers.values():
            self.server_box.addItem(server.name, server.id)
        index = self.server_box.findData(previous)
        self.server_box.setCurrentIndex(max(index, 0))
        self.server_box.blockSignals(False)
        self._rebuild_form()

    def _reload_servers(self, initial: bool = False) -> None:
        local_result = load_servers(include_urls=False)
        self._last_load_result = local_result
        self._apply_server_map(local_result.servers)

        self._servers_gen += 1
        gen = self._servers_gen

        def worker() -> None:
            full_result = load_servers(include_urls=True)
            self.servers_ready.emit(gen, full_result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_servers_ready(self, generation: int, result: LoadResult) -> None:
        if generation != self._servers_gen:
            return
        self._last_load_result = result
        if set(result.servers.keys()) != set(self._servers.keys()) or any(
            result.servers[k] != self._servers[k] for k in result.servers
        ):
            self._apply_server_map(result.servers)
        if result.errors:
            text = "\n".join(result.errors.values())
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Server definitions", text))

    def _open_servers_modal(self) -> None:
        dialog = ServersDialog(self, self._last_load_result)
        dialog.exec()
        if dialog.servers_changed:
            self._reload_servers()

    def _open_base_url(self) -> None:
        server = self._current_server()
        if server is None or not server.base_url:
            return
        QDesktopServices.openUrl(QUrl(server.base_url))

    def _sync_secret_save_box(self, key: str, saved_value: str) -> None:
        box = self._save_boxes.get(key)
        widget = self._inputs.get(key)
        if box is None or not isinstance(widget, QLineEdit):
            return
        current = widget.text()
        is_saved = bool(saved_value) and current == saved_value
        box.blockSignals(True)
        if is_saved:
            box.setChecked(True)
            box.setEnabled(False)
        else:
            box.setEnabled(True)
            if self._saved_key_values.get(key) == current and current:
                box.setChecked(False)
            elif not current:
                box.setChecked(False)
        box.blockSignals(False)
        _apply_disabled_opacity(box)

    def _api_keys(self) -> None:
        dialog = KeysDialog(self, self._servers, self._icon_family)
        dialog.exec()
        if dialog.keys_changed:
            server = self._current_server()
            if server is not None:
                saved = load_keys(server.id)
                self._saved_key_values = {
                    k: v for k, v in saved.items() if k != (server.auth.credentials_key if server.auth else "")
                }
                for key in self._save_boxes:
                    widget = self._inputs.get(key)
                    if isinstance(widget, QLineEdit):
                        widget.blockSignals(True)
                        widget.setText(self._saved_key_values.get(key, ""))
                        widget.blockSignals(False)
                    self._sync_secret_save_box(key, self._saved_key_values.get(key, ""))
                self._sync_needs()

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _rebuild_form(self) -> None:
        self._clear_layout(self.form_host)
        self._inputs.clear()
        self._save_boxes.clear()
        self._saved_key_values.clear()
        self._user_checked.clear()
        server = self._current_server()

        if server is not None and server.base_url:
            self.open_base_url_btn.setEnabled(True)
            self.open_base_url_btn.setToolTip(f"Open {server.base_url} in browser")
        else:
            self.open_base_url_btn.setEnabled(False)
            self.open_base_url_btn.setToolTip("")

        if server is None:
            self.form_host.addWidget(
                QLabel("No server definitions found. Click 'Servers' to add a server definition file or URL.")
            )
            self._sync_controls()
            return

        saved = load_keys(server.id)
        self._saved_key_values = {
            k: v for k, v in saved.items() if k != (server.auth.credentials_key if server.auth else "")
        }
        columns: dict[int, list[Field]] = {}
        for field in server.fields:
            columns.setdefault(field.column, []).append(field)

        for col_index in sorted(columns):
            col = QVBoxLayout()
            col.setSpacing(6)
            for field in columns[col_index]:
                self._add_field(col, field, saved)
            col.addStretch(1)
            self.form_host.addLayout(col, 0)

        self.form_host.addStretch(1)
        self._sync_needs()

    def _add_field(self, column: QVBoxLayout, field: Field, saved: dict[str, str]) -> None:
        if field.widget == "capture_table":
            header_row = QHBoxLayout()
            header_row.addWidget(QLabel(field.caption))
            if field.help:
                hint = QLabel(field.help)
                hint.setObjectName("hint")
                header_row.addWidget(hint)
            header_row.addStretch(1)
            column.addLayout(header_row)

            server = self._current_server()
            store = self._captured_values.setdefault(server.id, {}) if server else {}
            cap_widget = CaptureTableWidget(field, self)
            cap_widget.setFixedWidth(SERVER_FIELD_WIDTH)
            cap_widget.set_captured_values(store)
            cap_widget.save_token_requested.connect(self._on_save_captured_token)
            self._inputs[field.key] = cap_widget
            column.addWidget(cap_widget)
            return

        if field.widget == "urlButton":
            btn = QPushButton(field.caption)
            btn.setFixedSize(SERVER_FIELD_WIDTH, CONTROL_HEIGHT)
            btn.setToolTip(field.help or field.url)
            btn.clicked.connect(lambda _=False, f=field: self._on_url_button_clicked(f))
            track_disabled_opacity(btn)
            self._inputs[field.key] = btn
            column.addWidget(btn)
            if field.help:
                hint = QLabel(field.help)
                hint.setObjectName("hint")
                column.addWidget(hint)
            return

        if field.widget == "button":
            server = self._current_server()
            caption = field.caption
            if field.action and field.action.action_type == "oauth2" and server and server.auth:
                try:
                    if access_token(server):
                        caption = "Deauthorize"
                except OAuthError:
                    pass
            btn = QPushButton(caption)
            btn.setFixedSize(SERVER_FIELD_WIDTH, CONTROL_HEIGHT)
            if field.help:
                btn.setToolTip(field.help)
            btn.clicked.connect(lambda _=False, f=field: self._on_button_clicked(f))
            track_disabled_opacity(btn)
            self._inputs[field.key] = btn
            column.addWidget(btn)
            if field.help:
                hint = QLabel(field.help)
                hint.setObjectName("hint")
                column.addWidget(hint)
            return

        if field.widget == "checkbox":
            box = QCheckBox(field.caption)
            box.setFixedWidth(SERVER_FIELD_WIDTH)
            if field.help:
                box.setToolTip(field.help)
            self._user_checked[field.key] = False
            box.toggled.connect(lambda checked, k=field.key: self._on_checkbox_toggled(k, checked))
            track_disabled_opacity(box)
            self._inputs[field.key] = box
            column.addWidget(box)
            return

        label_row = QHBoxLayout()
        label_row.addWidget(QLabel(field.caption))
        if field.help:
            hint = QLabel(field.help)
            hint.setObjectName("hint")
            label_row.addWidget(hint)
        label_row.addStretch(1)
        column.addLayout(label_row)

        if field.widget == "notes":
            widget = QPlainTextEdit()
            server = self._current_server()
            widget.setPlainText(render_notes(field, self._values(), server.base_url if server else ""))
            widget.setReadOnly(True)
            widget.setFixedWidth(SERVER_FIELD_WIDTH)
            widget.setMinimumHeight(TEXTAREA_MIN_HEIGHT)
            widget.setMaximumHeight(TEXTAREA_MAX_HEIGHT)
            track_disabled_opacity(widget)
            self._inputs[field.key] = widget
            column.addWidget(widget)
        elif field.widget == "textarea":
            widget = QPlainTextEdit()
            widget.setPlaceholderText(field.caption)
            widget.setFixedWidth(SERVER_FIELD_WIDTH)
            widget.setMinimumHeight(TEXTAREA_MIN_HEIGHT)
            widget.setMaximumHeight(TEXTAREA_MAX_HEIGHT)
            widget.textChanged.connect(self._sync_needs)
            track_disabled_opacity(widget)
            self._inputs[field.key] = widget
            column.addWidget(widget)
        elif field.widget == "select":
            widget = IconComboBox(self._icon_family)
            widget.setFixedSize(SERVER_FIELD_WIDTH, CONTROL_HEIGHT)
            for value, caption in field.choices:
                widget.addItem(caption, value)
            widget.currentIndexChanged.connect(lambda _=0, k=field.key: self._on_field_committed(k))
            widget.currentIndexChanged.connect(self._sync_needs)
            track_disabled_opacity(widget)
            self._inputs[field.key] = widget
            column.addWidget(widget)
            if field.choices_from:
                self._load_choices(field)
        else:
            widget = QLineEdit()
            widget.setPlaceholderText(field.caption)
            widget.setFixedHeight(CONTROL_HEIGHT)
            widget.textChanged.connect(lambda _text, k=field.key: self._on_line_edit_changed(k))
            widget.editingFinished.connect(lambda k=field.key: self._on_field_committed(k))
            track_disabled_opacity(widget)
            self._inputs[field.key] = widget
            if field.widget == "secret":
                widget.setEchoMode(QLineEdit.EchoMode.Password)
                saved_val = saved.get(field.key, "")
                widget.setText(saved_val)
                toggle = self._icon_button("eye", "Show")
                toggle.setFixedSize(38, CONTROL_HEIGHT)
                toggle.clicked.connect(lambda _=False, w=widget, b=toggle: self._toggle_secret(w, b))

                gap = 6
                widget.setFixedWidth(SERVER_FIELD_WIDTH - toggle.width() - gap)

                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(gap)
                row.setAlignment(Qt.AlignmentFlag.AlignLeft)
                row.addWidget(widget)
                row.addWidget(toggle)
                column.addLayout(row)

                save_box = QCheckBox(field.save_label or f"Save {field.caption}")
                save_box.setFixedWidth(SERVER_FIELD_WIDTH)
                save_box.toggled.connect(lambda on, k=field.key: self._on_save_toggled(k, on))
                track_disabled_opacity(save_box)
                self._save_boxes[field.key] = save_box
                self._sync_secret_save_box(field.key, saved_val)
                column.addWidget(save_box)
            else:
                widget.setFixedWidth(SERVER_FIELD_WIDTH)
                column.addWidget(widget)

    def _on_line_edit_changed(self, key: str) -> None:
        if key in self._save_boxes:
            self._sync_secret_save_box(key, self._saved_key_values.get(key, ""))
        self._sync_needs()

    def _on_url_button_clicked(self, field: Field) -> None:
        server = self._current_server()
        if server is None or not field.url:
            return
        ctx = {k: str(v).strip() for k, v in self._values().items()}
        ctx["base_url"] = server.base_url
        target = resolve_url(server.base_url, render(field.url, ctx))
        QDesktopServices.openUrl(QUrl(target))

    def _on_button_clicked(self, field: Field) -> None:
        server = self._current_server()
        if server is None:
            return
        self._on_field_committed(field.key)
        action = field.action
        if action is None:
            return
        ctx = {k: str(v).strip() for k, v in self._values().items()}
        ctx["base_url"] = server.base_url

        if action.action_type == "oauth2":
            btn = self._inputs.get(field.key)
            if isinstance(btn, QPushButton):
                btn.setEnabled(False)
            provider = (server.auth.provider if server.auth else action.provider) or "OAuth"
            label = "Google" if provider.strip().lower() == "google" else provider.strip().capitalize()

            def work() -> None:
                try:
                    if server.auth and access_token(server):
                        disconnect_oauth(server)
                        message = f"{label} account deauthorized."
                    else:
                        authorize_oauth(server)
                        message = f"{label} account authorized."
                    self.button_action_done.emit(server.id, field.key, {}, message, "")
                except Exception as exc:
                    self.button_action_done.emit(server.id, field.key, {}, "", str(exc))

            threading.Thread(target=work, daemon=True).start()
            return

        if action.action_type == "open_url":
            target = resolve_url(server.base_url, render(action.url or field.url, ctx))
            if target:
                QDesktopServices.openUrl(QUrl(target))
            return

        if action.action_type == "copy":
            rendered = render(action.text, ctx)
            if rendered:
                QGuiApplication.clipboard().setText(rendered)
            return

        if action.action_type == "clear":
            store = self._captured_values.setdefault(server.id, {})
            for target_key in action.targets:
                store.pop(target_key, None)
                widget = self._inputs.get(target_key)
                if isinstance(widget, QLineEdit):
                    widget.clear()
                elif isinstance(widget, QPlainTextEdit):
                    widget.clear()
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(False)
            for widget in self._inputs.values():
                if isinstance(widget, CaptureTableWidget):
                    widget.set_captured_values(store)
            self._sync_needs()
            return

        if action.action_type == "request":
            btn = self._inputs.get(field.key)
            if isinstance(btn, QPushButton):
                btn.setEnabled(False)
            values_snapshot = self._values()

            def work() -> None:
                try:
                    captured, msg = execute_button_action(server, field, values_snapshot)
                    self.button_action_done.emit(server.id, field.key, captured, msg, "")
                except Exception as exc:
                    self.button_action_done.emit(server.id, field.key, {}, "", str(exc))

            threading.Thread(target=work, daemon=True).start()

    def _on_button_action_done(
        self, server_id: str, field_key: str, captured: dict[str, str], message: str, error: str
    ) -> None:
        server = self._current_server()
        if server is None or server.id != server_id:
            return
        self._sync_needs()
        if error:
            QMessageBox.warning(self, server.name, error)
            button = self._inputs.get(field_key)
            if isinstance(button, QPushButton):
                button.setEnabled(True)
            return
        if captured:
            store = self._captured_values.setdefault(server.id, {})
            for k, v in captured.items():
                widget = self._inputs.get(k)
                if isinstance(widget, QLineEdit):
                    widget.setText(v)
                else:
                    store[k] = v
            for widget in self._inputs.values():
                if isinstance(widget, CaptureTableWidget):
                    widget.set_captured_values(store)
            self._sync_needs()

        field_def = server.field(field_key)
        if field_def and field_def.action:
            for rf in field_def.action.reload_fields:
                target_field = server.field(rf)
                if target_field and target_field.choices_from:
                    self._load_choices(target_field)
        button = self._inputs.get(field_key)
        if isinstance(button, QPushButton):
            button.setEnabled(True)
            if field_def and field_def.action and field_def.action.action_type == "oauth2":
                authorized = False
                try:
                    authorized = bool(server.auth and access_token(server))
                except OAuthError:
                    authorized = False
                button.setText("Deauthorize" if authorized else field_def.caption)
        if message:
            QMessageBox.information(self, server.name, message)

    def _on_save_captured_token(self, target_key: str, token_value: str) -> None:
        server = self._current_server()
        if server is None or not token_value.strip():
            return
        clean = token_value.strip()
        try:
            save_key(server.id, target_key, clean)
        except OSError as exc:
            QMessageBox.warning(self, "API Keys", f"Could not save the token: {exc}")
            return
        self._saved_key_values[target_key] = clean
        widget = self._inputs.get(target_key)
        if isinstance(widget, QLineEdit):
            widget.setText(clean)
        self._sync_secret_save_box(target_key, clean)
        self._sync_needs()

    def _on_checkbox_toggled(self, key: str, checked: bool) -> None:
        self._user_checked[key] = checked
        self._sync_needs()

    def _on_max_uploads_changed(self, value: int) -> None:
        clamped = max(MIN_MAX_UPLOADS, min(MAX_MAX_UPLOADS, int(value)))
        self.max_uploads_apply_btn.setEnabled(clamped != self._max_uploads)

    def _apply_max_uploads(self) -> None:
        clamped = max(MIN_MAX_UPLOADS, min(MAX_MAX_UPLOADS, self.max_uploads_spin.value()))
        self._max_uploads = clamped
        self.uploader.set_max_uploads(clamped)
        _save_ui_state({"max_uploads": clamped})
        self.max_uploads_apply_btn.setEnabled(False)

    def _toggle_secret(self, edit: QLineEdit, button: QPushButton) -> None:
        hidden = edit.echoMode() == QLineEdit.EchoMode.Password
        edit.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        apply_icon(button, self._icon_family, "eye_slash" if hidden else "eye")
        button.setToolTip("Hide" if hidden else "Show")

    def _on_save_toggled(self, key: str, checked: bool) -> None:
        server = self._current_server()
        if server is None:
            return
        if not checked:
            delete_key(server.id, key)
            self._saved_key_values.pop(key, None)
        elif (value := self._values().get(key, "").strip()):
            save_key(server.id, key, value)
            self._saved_key_values[key] = value
            self._sync_secret_save_box(key, value)

    def _values(self) -> dict[str, str]:
        server = self._current_server()
        out: dict[str, str] = {}
        if server is not None and server.id in self._captured_values:
            out.update(self._captured_values[server.id])
        for key, widget in self._inputs.items():
            if not widget.isEnabled():
                out[key] = ""
            elif isinstance(widget, QLineEdit):
                out[key] = widget.text()
            elif isinstance(widget, QPlainTextEdit):
                out[key] = widget.toPlainText()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentData() or ""
            elif isinstance(widget, QCheckBox):
                out[key] = "true" if widget.isChecked() else ""
        return out

    def _sync_needs(self) -> None:
        server = self._current_server()
        if server is None:
            self._sync_controls()
            return
        selected_rows = self.table.selectionModel().selectedRows()

        if selected_rows:
            multiple_selected = len(selected_rows) > 1
        else:
            multiple_selected = len(self.uploader.jobs) > 1
        for _ in range(3):
            changed = False
            values = self._values()
            for field in server.fields:
                widget = self._inputs.get(field.key)
                if widget is None:
                    continue
                if isinstance(widget, QCheckBox):
                    forced = server.is_field_forced_checked(field, values)
                    if forced:
                        if not widget.isChecked():
                            widget.blockSignals(True)
                            widget.setChecked(True)
                            widget.blockSignals(False)
                            changed = True
                    else:
                        desired = self._user_checked.get(field.key, False)
                        if widget.isChecked() != desired:
                            widget.blockSignals(True)
                            widget.setChecked(desired)
                            widget.blockSignals(False)
                            changed = True
                enabled = server.is_field_enabled(field, values, multiple_selected)
                if widget.isEnabled() != enabled:
                    widget.setEnabled(enabled)
                    if not enabled:
                        if isinstance(widget, QLineEdit) and widget.text():
                            widget.clear()
                            changed = True
                        elif isinstance(widget, QPlainTextEdit) and widget.toPlainText():
                            widget.clear()
                            changed = True
                        elif isinstance(widget, QCheckBox) and widget.isChecked():
                            widget.blockSignals(True)
                            widget.setChecked(False)
                            widget.blockSignals(False)
                            changed = True
            if not changed:
                break

        self._sync_controls()

    def _action_ids(self) -> list[int]:
        """Jobs the toolbar acts on: the selected rows, or every row when none is selected."""
        return self._selected_ids() or list(self._job_states)

    def _sync_controls(self) -> None:
        selected_ids = self._selected_ids()
        has_selection = bool(selected_ids)
        self.remove_btn.setEnabled(has_selection)
        can_reorder = has_selection
        self.move_down_btn.setEnabled(can_reorder)
        self.move_up_btn.setEnabled(can_reorder)

        states = [self._job_states.get(jid, State.READY) for jid in self._action_ids()]
        scope = "selected" if has_selection else "all"
        can_start = any(st in (State.READY, State.FAILED) for st in states)
        can_stop = any(st in RUNNING_STATES for st in states)
        can_restart = any(st in (State.DONE, State.FAILED) for st in states)
        self.start_btn.setEnabled(can_start)
        self.stop_btn.setEnabled(can_stop)
        self.restart_btn.setEnabled(can_restart)
        self.start_btn.setToolTip("Start selected uploads" if has_selection else "Start all pending uploads")
        self.stop_btn.setToolTip("Stop selected uploads" if has_selection else "Stop all running uploads")

        if can_restart:
            tip = f"Upload {scope} finished files again to the selected server (each gets a new link)"
        elif not states:
            tip = "Add files to upload first"
        elif State.UPLOADING in states:
            tip = "Not available while a file is uploading. Stop it first"
        elif State.PROCESSING in states:
            tip = "Not available while a file is being prepared. Stop it first"
        elif State.WAITING in states:
            tip = "Not available while a file is waiting in the queue. Stop it first"
        else:
            tip = "Available once an upload has finished (done or failed)"
        self.restart_btn.setToolTip(tip)

        has_urls = bool(self.uploader.urls())
        self.copy_btn.setEnabled(has_urls)
        self.save_btn.setEnabled(has_urls)

        any_uploading = State.UPLOADING in self._job_states.values()
        if any_uploading and not self._eta_timer.isActive():
            self._eta_timer.start()
        elif not any_uploading and self._eta_timer.isActive():
            self._eta_timer.stop()

    def _refresh_active_etas(self) -> None:
        was_sorting = self.table.isSortingEnabled()
        if was_sorting:
            self.table.setSortingEnabled(False)
        for job in self.uploader.jobs:
            if job.id in self._removed_ids:
                continue
            row = self._row_of.get(job.id)
            if row is not None and row < self.table.rowCount():
                eta_item = self.table.item(row, C_ETA)
                if eta_item is not None:
                    eta_item.setText(human_eta(job.eta))
                    eta_item.setData(SORT_ROLE, job.eta)
        if was_sorting:
            self.table.setSortingEnabled(True)
            self._sort_and_reindex_upload_rows()

    def _on_field_committed(self, key: str) -> None:
        server = self._current_server()
        if server is None:
            return
        for field in server.fields:
            if field.choices_from and key in field.choices_from.reload_on:
                self._load_choices(field)

    def _load_choices(self, field: Field) -> None:
        server = self._current_server()
        if server is None:
            return
        generation = self._choice_gen.get(field.key, 0) + 1
        self._choice_gen[field.key] = generation
        values = self._values()
        base_url = server.base_url

        def work() -> None:
            problems: list[str] = []
            items = fetch_choices(field, values, problems.append, base_url=base_url)
            self.choices_ready.emit(server.id, field.key, generation, items, problems[0] if problems else "")

        threading.Thread(target=work, daemon=True).start()

    def _on_choices(self, server_id: str, key: str, generation: int, items, error: str) -> None:
        current = self._current_server()
        combo = self._inputs.get(key)
        if (current is None or current.id != server_id or generation != self._choice_gen.get(key)
                or not isinstance(combo, QComboBox)):
            return
        selected = combo.currentData()
        combo.clear()
        for value, caption in items:
            combo.addItem(caption, value)
        combo.setCurrentIndex(max(combo.findData(selected), 0))
        combo.setFixedSize(SERVER_FIELD_WIDTH, CONTROL_HEIGHT)
        combo.setToolTip(f"Could not load options: {error}" if error else "")
        if error:
            print(f"[drainpype] {server_id}.{key}: {error}", file=sys.stderr)

    def _hint(server: Server, field: Field) -> str:
        pieces = []
        if (
            not field.mandatory
            and field.widget not in ("checkbox", "notes")
        ):
            pieces.append("Optional")
        extra = field.help
        if not extra and field.needs:
            needed = server.field(field.needs)
            extra = f"Requires {needed.caption if needed else field.needs}"
        if extra:
            pieces.append(extra)
        return f"({': '.join(pieces)})" if pieces else ""

    def _make_input(self, field: Field) -> QWidget:
        if field.widget == "textarea":
            server = self._current_server()
            widget = QPlainTextEdit()
            widget.setMinimumHeight(TEXTAREA_MIN_HEIGHT)
            widget.setMaximumHeight(TEXTAREA_MAX_HEIGHT)
            widget.setPlainText(
                render_textarea(field, self._values(), server.base_url if server else "")
            )
            widget.setReadOnly(not field.editable)

    # ------------------------------------------------------------------ files + queue
    def _add_files(self) -> None:
        start_dir = self._last_files_dir if self._last_files_dir and Path(self._last_files_dir).is_dir() else ""
        paths, _ = QFileDialog.getOpenFileNames(self, "Select files", start_dir)
        if paths:
            self._last_files_dir = str(Path(paths[0]).parent)
            _save_ui_state({"last_files_dir": self._last_files_dir})
            self.uploader.add_files(paths)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._last_files_dir = str(Path(paths[0]).parent)
            _save_ui_state({"last_files_dir": self._last_files_dir})
            self.uploader.add_files(paths)

    def _selected_ids(self) -> list[int]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [self.table.item(r, C_NAME).data(Qt.ItemDataRole.UserRole) for r in sorted(rows)]

    def _remove_selected(self) -> None:
        selected = self._selected_ids()
        if not selected:
            return
        removed = self.uploader.remove(selected)
        self._removed_ids.update(removed)
        for jid in removed:
            self._job_states.pop(jid, None)
        was_sorting = self.table.isSortingEnabled()
        if was_sorting:
            self.table.setSortingEnabled(False)
        for row in sorted((self._row_of[i] for i in removed if i in self._row_of), reverse=True):
            self.table.removeRow(row)
        self._reindex_upload_rows()
        self._renumber_upload_rows()
        if was_sorting:
            self.table.setSortingEnabled(True)
            self._sort_and_reindex_upload_rows()
        self._update_stats()
        self._sync_needs()

    def _move_selected_rows(self, direction: int) -> None:
        selected_ids = self._selected_ids()
        if not selected_ids:
            return
        if self.uploader.move_jobs(selected_ids, direction):
            self._refresh_table_order(set(selected_ids))

    def _refresh_table_order(self, selected_ids: set[int]) -> None:
        """Rebuild queue rows while retaining selected job IDs and safely recreating widgets."""
        rows: dict[int, list[QTableWidgetItem | None]] = {}
        table = self.table
        selection_model = table.selectionModel()
        was_sorting = table.isSortingEnabled()
        if was_sorting:
            table.setSortingEnabled(False)
        selection_model.blockSignals(True)
        table.blockSignals(True)
        for row in range(table.rowCount()):
            name_item = table.item(row, C_NAME)
            if name_item is None:
                continue
            job_id = name_item.data(Qt.ItemDataRole.UserRole)
            rows[job_id] = [table.takeItem(row, col) for col in range(table.columnCount())]

        while table.rowCount():
            table.removeRow(table.rowCount() - 1)
        self._row_of.clear()
        for job in self.uploader.jobs:
            items = rows.get(job.id)
            if items is None:
                continue
            row = table.rowCount()
            table.insertRow(row)
            self._row_of[job.id] = row
            for col, item in enumerate(items):
                if item is not None:
                    table.setItem(row, col, item)
            # The URL is rendered by the selectable QLineEdit cell widget below.
            # Keep the underlying item blank so Qt does not paint the URL twice.
            url_item = table.item(row, C_URL)
            if url_item is not None:
                url_item.setText("")
            url_edit = self._make_url_widget()
            url_edit.setText(job.url or job.error)
            url_edit.setCursorPosition(0)
            color = "#ef4444" if job.state is State.FAILED else "#eceff4"
            url_edit.setStyleSheet(
                f"QLineEdit {{ background: transparent; border: none; border-radius: 0px;"
                f" padding: 0px 8px; color: {color};"
                f" selection-background-color: #2563d9; selection-color: #ffffff; }}"
            )
            url_edit.installEventFilter(RowClickFilter(table, url_edit))
            table.setCellWidget(row, C_URL, url_edit)

        self._renumber_upload_rows()
        table.blockSignals(False)
        if was_sorting:
            table.setSortingEnabled(True)
            self._sort_and_reindex_upload_rows()
        selection_model.blockSignals(False)
        for job_id in selected_ids:
            row = self._row_of.get(job_id)
            if row is not None:
                selection_model.select(
                    table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
        self._sync_needs()

    def _prepare_run(self) -> tuple[Server, dict[str, str]] | None:
        """Validate the server options (and save keys that were asked for) before a run."""
        server = self._current_server()
        if server is None:
            QMessageBox.warning(self, "No server", "Add a server definition first via 'Servers'.")
            return None
        values = self._values()
        problems = server.check_values(values)
        if problems:
            QMessageBox.warning(self, "Server options", "\n".join(problems))
            return None
        try:
            for key, box in self._save_boxes.items():
                if box.isChecked() and values.get(key, "").strip():
                    clean = values[key].strip()
                    save_key(server.id, key, clean)
                    self._saved_key_values[key] = clean
                    self._sync_secret_save_box(key, clean)
        except OSError as exc:
            QMessageBox.warning(self, "API Keys", f"Could not save the key: {exc}")
        return server, values

    def _launch(self, action) -> None:
        try:
            action()
        except CurlNotFoundError as exc:
            QMessageBox.warning(self, "curl not found", str(exc))
        except ServerBusyError as exc:
            QMessageBox.information(self, "Uploads in progress", str(exc))

    def _start(self) -> None:
        target_ids = [
            jid for jid in self._action_ids()
            if self._job_states.get(jid, State.READY) in (State.READY, State.FAILED)
        ]
        if not target_ids:
            return
        run = self._prepare_run()
        if run is None:
            return
        server, values = run
        self._launch(lambda: self.uploader.start(server, values, job_ids=target_ids))

    def _restart(self) -> None:
        target_ids = [
            jid for jid in self._action_ids()
            if self._job_states.get(jid) in (State.DONE, State.FAILED)
        ]
        if not target_ids:
            return
        run = self._prepare_run()
        if run is None:
            return
        server, values = run
        self._launch(lambda: self.uploader.restart(server, values, job_ids=target_ids))

    def _stop(self) -> None:
        target_ids = [
            jid for jid in self._action_ids()
            if self._job_states.get(jid, State.READY) in RUNNING_STATES
        ]
        if not target_ids:
            return
        self.uploader.stop(job_ids=target_ids)

    # ------------------------------------------------------------------ table updates
    def _make_url_widget(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setReadOnly(True)
        edit.setFrame(False)
        edit.setStyleSheet(
            "QLineEdit { background: transparent; border: none; border-radius: 0px;"
            " padding: 0px 8px; color: #eceff4;"
            " selection-background-color: #2563d9; selection-color: #ffffff; }"
        )
        return edit

    def _on_job(self, job) -> None:
        if job.id in self._removed_ids:
            return
        was_sorting = self.table.isSortingEnabled()
        if was_sorting:
            self.table.setSortingEnabled(False)
        self._job_states[job.id] = job.state
        row = self._row_of.get(job.id)
        is_new_row = row is None
        if is_new_row:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_of[job.id] = row
            for col in range(len(COLUMNS)):
                item = SortableTableWidgetItem()
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, col, item)
            number = next((i for i, queued in enumerate(self.uploader.jobs, start=1)
                           if queued.id == job.id), row + 1)
            self.table.item(row, C_NUMBER).setText(str(number))
            self.table.item(row, C_NUMBER).setData(SORT_ROLE, number)
            self.table.item(row, C_NAME).setData(Qt.ItemDataRole.UserRole, job.id)
            self.table.item(row, C_NAME).setText(job.name)
            url_widget = self._make_url_widget()
            url_widget.installEventFilter(RowClickFilter(self.table, url_widget))
            self.table.setCellWidget(row, C_URL, url_widget)

        name_item = self.table.item(row, C_NAME)
        name_tip = f"{job.path}\nType: {job.mime}"
        if name_item.toolTip() != name_tip:
            name_item.setToolTip(name_tip)
        size_item = self.table.item(row, C_SIZE)
        size_item.setText(human_size(job.size))
        size_item.setData(SORT_ROLE, job.size)
        server_item = self.table.item(row, C_SERVER)
        server_item.setText(job.server_name)
        if job.server_name:
            server_item.setToolTip(job.server_name)

        status = self.table.item(row, C_STATUS)
        status.setText(job.state.value.capitalize())
        status.setForeground(QColor(STATE_COLORS.get(job.state, "#eceff4")))
        status.setToolTip(job.error)

        sent_item = self.table.item(row, C_SENT)
        sent_item.setText(human_size(job.sent))
        sent_item.setData(SORT_ROLE, job.sent)
        progress_item = self.table.item(row, C_PROGRESS)
        progress_item.setData(Qt.ItemDataRole.UserRole, job.progress)
        progress_item.setData(SORT_ROLE, job.progress)
        progress_item.setToolTip(
            f"Processing: {int(job.progress * 100)}%" if job.state is State.PROCESSING
            else f"Uploading: {int(job.progress * 100)}%" if job.state is State.UPLOADING
            else ""
        )
        speed_item = self.table.item(row, C_SPEED)
        speed_item.setText(human_speed(job.speed) if job.state is State.UPLOADING else "")
        speed_item.setData(SORT_ROLE, job.speed)

        url_edit = self.table.cellWidget(row, C_URL)
        display_text = job.url or job.error
        # The selectable cell widget owns the visible URL text; the backing item
        # must stay blank to avoid drawing the same text underneath it.
        url_item = self.table.item(row, C_URL)
        if url_item is not None:
            url_item.setData(SORT_ROLE, display_text.casefold())
            if url_item.text():
                url_item.setText("")
        if isinstance(url_edit, QLineEdit):
            if url_edit.text() != display_text:
                url_edit.setText(display_text)
                url_edit.setCursorPosition(0)
            color = "#ef4444" if job.state is State.FAILED else "#eceff4"
            url_edit.setStyleSheet(
                f"QLineEdit {{ background: transparent; border: none; border-radius: 0px;"
                f" padding: 0px 8px; color: {color};"
                f" selection-background-color: #2563d9; selection-color: #ffffff; }}"
            )

        eta_item = self.table.item(row, C_ETA)
        eta_item.setText(human_eta(job.eta))
        eta_item.setData(SORT_ROLE, job.eta)

        if job.state is State.DONE and job.captured:
            server = job.server
            if server is not None:
                store = self._captured_values.setdefault(server.id, {})
                store.update(job.captured)
                current = self._current_server()
                if current is not None and current.id == server.id:
                    for widget in self._inputs.values():
                        if isinstance(widget, CaptureTableWidget):
                            widget.set_captured_values(store)

        if was_sorting:
            self.table.setSortingEnabled(True)
            self._sort_and_reindex_upload_rows()
        self.table.viewport().update()
        self._update_stats()
        if is_new_row:
            self._sync_needs()
        else:
            self._sync_controls()

    def _update_stats(self) -> None:
        total, done, failed = self.uploader.counts()
        self.stats.setText(f"Total Files: {total} | {done} Completed | {failed} Failed")

    def _copy_urls(self) -> None:
        urls = self.uploader.urls()
        if urls:
            QGuiApplication.clipboard().setText("\n".join(urls))

    def _save_urls(self) -> None:
        urls = self.uploader.urls()
        if not urls:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save URLs", "drainpype-urls.txt", "Text files (*.txt)")
        if path:
            Path(path).write_text("\n".join(urls) + "\n", encoding="utf-8")

    def closeEvent(self, event) -> None:
        if any(state in RUNNING_STATES for state in self._job_states.values()):
            answer = QMessageBox.question(
                self, "Uploads in progress", "Uploads are still running. Stop them and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self._resize_save_timer.isActive():
            self._resize_save_timer.stop()
        if self._layout_save_timer.isActive():
            self._layout_save_timer.stop()
        if self._eta_timer.isActive():
            self._eta_timer.stop()
        self.uploader.stop()
        super().closeEvent(event)


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    palette = QPalette()
    for role, colour in (
        (QPalette.ColorRole.Window, "#2e3440"), (QPalette.ColorRole.WindowText, "#eceff4"),
        (QPalette.ColorRole.Base, "#20242C"), (QPalette.ColorRole.Text, "#eceff4"),
        (QPalette.ColorRole.Button, "#20242C"), (QPalette.ColorRole.ButtonText, "#eceff4"),
        (QPalette.ColorRole.Highlight, "#2563d9"), (QPalette.ColorRole.HighlightedText, "#ffffff"),
    ):
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)

    win_icon = _resolve_window_icon()
    if win_icon is not None:
        app.setWindowIcon(win_icon)

    check_rule = _ensure_checkmark_icon()
    icon_family = load_icon_font()
    spin_up, spin_down = _ensure_spin_arrow_icons(icon_family)
    app.setStyleSheet(
        STYLE_TEMPLATE.replace("__CHECK_IMAGE__", check_rule)
        .replace("__SPIN_UP_IMAGE__", spin_up)
        .replace("__SPIN_DOWN_IMAGE__", spin_down)
    )

    window = MainWindow()
    if window.starts_maximized():
        window.showMaximized()
    else:
        window.show()
    return app.exec()
