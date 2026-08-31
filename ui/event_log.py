"""
ui/event_log.py — Scrollable list of lag events on the left panel.

Each row shows:
  - Severity colour indicator
  - Time of event
  - Duration
  - Cause code badge
"""
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea,
    QFrame, QHBoxLayout, QPushButton,
    QButtonGroup,
)

from core.models import LagEvent

GREEN  = "#2ecc71"
AMBER  = "#f39c12"
RED    = "#e74c3c"
BG     = "#0d1117"
BG2    = "#161b22"
BG3    = "#21262d"
TEXT   = "#e6edf3"
MUTED  = "#8b949e"
ACCENT = "#58a6ff"

CAUSE_COLOURS = {
    "CPU_SPIKE":            RED,
    "RAM_EXHAUSTION":       "#9b59b6",
    "RAM_PRESSURE":         AMBER,
    "BACKGROUND_CLUSTER":   AMBER,
    "DISK_IO":              "#1abc9c",
    "SCHEDULER_CONTENTION": MUTED,
    "CPU_BOUND":            RED,
    "GPU_BOUND":            ACCENT,
    "VRAM_PRESSURE":        "#1abc9c",
    "DRIVER_RENDER_PATH":   ACCENT,
    "IO_STALL":             "#1abc9c",
    "BACKGROUND_INTERFERENCE": AMBER,
    "SYSTEM_RAM_PRESSURE":  RED,
    "GAME_MEMORY_LIMIT":    "#9b59b6",
    "LOCAL_STUTTER":        AMBER,
    "UNDETERMINED":         MUTED,
    "REPORT_PENDING":       ACCENT,
    "FRAME_SPIKE":          ACCENT,
    "FRAME_STUTTER":        "#e67e22",
    "FRAME_FREEZE":         RED,
    "Window Not Responding": RED,
    "Visual Freeze":        ACCENT,
    "Responsiveness Stall": AMBER,
    "CPU Pressure Stall":   RED,
    "I/O Pressure Stall":   "#1abc9c",
    "RESOURCE_PRESSURE_RISK": AMBER,
    "UNKNOWN":              MUTED,
}


def _severity_colour(score: float) -> str:
    if score < 0.5:
        return AMBER
    if score < 0.75:
        return "#e67e22"
    return RED


class EventRow(QFrame):
    """Single clickable row in the event log."""

    clicked = Signal(object)   # emits LagEvent
    delete_requested = Signal(object)  # emits LagEvent

    def __init__(self, event: LagEvent, parent=None):
        super().__init__(parent)
        self._lag_event = event
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._set_style(False)

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # Colour dot
        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)

        # Time + duration
        info = QVBoxLayout()
        info.setSpacing(2)
        self._time_label = QLabel()
        self._time_label.setTextFormat(Qt.TextFormat.RichText)
        self._time_label.setStyleSheet(f"color: {TEXT}; font-size: 12px; font-weight: 600;")
        self._dur_label = QLabel()
        self._dur_label.setStyleSheet(f"color: {MUTED}; font-size: 10px;")
        info.addWidget(self._time_label)
        info.addWidget(self._dur_label)

        # Cause badge
        self._badge = QLabel()
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        delete_btn = QPushButton("×")
        delete_btn.setFixedSize(22, 22)
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED};
                background: transparent;
                border: 1px solid #30363d;
                border-radius: 11px;
                font-size: 12px;
                font-weight: 700;
                padding: 0;
            }}
            QPushButton:hover {{
                color: {TEXT};
                border-color: #58a6ff;
                background: {BG3};
            }}
        """)
        delete_btn.clicked.connect(self._on_delete_clicked)

        layout.addWidget(self._dot)
        layout.addLayout(info, stretch=1)
        layout.addWidget(self._badge)
        layout.addWidget(delete_btn)
        self.refresh()

    def _set_style(self, selected: bool):
        bg = BG3 if selected else BG2
        self.setStyleSheet(f"""
            EventRow {{
                background: {bg};
                border-radius: 6px;
                border: 1px solid {'#58a6ff' if selected else '#30363d'};
            }}
            EventRow:hover {{
                background: {BG3};
                border-color: #444c56;
            }}
        """)

    def set_selected(self, val: bool):
        self._selected = val
        self._set_style(val)

    def refresh(self):
        time_str = self._lag_event.started_at.strftime("%H:%M:%S")
        date_str = self._lag_event.started_at.strftime("%b %d")
        dur = round(self._lag_event.duration_seconds, 1)
        self._time_label.setText(f"{time_str}  <span style='color:{MUTED};font-size:10px'>{date_str}</span>")
        if self._lag_event.is_pending:
            self._dur_label.setText("正在生成报告")
        elif self._lag_event.category == "RESOURCE_PRESSURE_RISK":
            self._dur_label.setText("资源压力提示")
        else:
            self._dur_label.setText(f"{dur}s duration")
        code = self._lag_event.category or self._lag_event.cause_code or "UNKNOWN"
        badge_colour = CAUSE_COLOURS.get(code, MUTED)
        self._badge.setText(code.replace("_", " "))
        self._badge.setStyleSheet(f"""
            color: {badge_colour};
            background: transparent;
            border: 1px solid {badge_colour};
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 9px;
            font-weight: 700;
        """)
        colour = CAUSE_COLOURS.get(code, _severity_colour(self._lag_event.peak_composite_score))
        self._dot.setStyleSheet(f"color: {colour}; font-size: 10px;")

    def _on_delete_clicked(self):
        self.delete_requested.emit(self._lag_event)

    def mousePressEvent(self, event):
        self.clicked.emit(self._lag_event)
        super().mousePressEvent(event)


class EventLogWidget(QWidget):
    """
    Left panel: scrollable list of EventRow widgets.
    Emits event_selected(LagEvent) when a row is clicked.
    """

    event_selected = Signal(object)   # LagEvent
    event_delete_requested = Signal(object)  # LagEvent
    clear_all_requested = Signal(str)  # "stutter", "minor", or "pressure"
    more_history_requested = Signal()
    filter_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[EventRow] = []
        self._selected_row: EventRow | None = None
        self._total_count = 0
        self._loading_more = False
        self._filter_mode = "stutter"
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("卡顿事件")
        title.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px;")

        filter_style = f"""
            QPushButton {{
                color: {MUTED};
                background: transparent;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                color: {TEXT};
                border-color: #58a6ff;
                background: {BG3};
            }}
            QPushButton:pressed {{
                background: #264f78;
                border-color: #58a6ff;
            }}
            QPushButton:checked {{
                color: {BG};
                background: {ACCENT};
                border-color: {ACCENT};
            }}
        """
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._stutter_filter_btn = QPushButton("卡顿报告")
        self._stutter_filter_btn.setCheckable(True)
        self._stutter_filter_btn.setChecked(True)
        self._stutter_filter_btn.setStyleSheet(filter_style)
        self._minor_filter_btn = QPushButton("轻微干扰")
        self._minor_filter_btn.setCheckable(True)
        self._minor_filter_btn.setStyleSheet(filter_style)
        self._pressure_filter_btn = QPushButton("系统压力")
        self._pressure_filter_btn.setCheckable(True)
        self._pressure_filter_btn.setStyleSheet(filter_style)
        self._filter_group.addButton(self._stutter_filter_btn)
        self._filter_group.addButton(self._minor_filter_btn)
        self._filter_group.addButton(self._pressure_filter_btn)
        self._stutter_filter_btn.clicked.connect(lambda: self._on_filter_changed("stutter"))
        self._minor_filter_btn.clicked.connect(lambda: self._on_filter_changed("minor"))
        self._pressure_filter_btn.clicked.connect(lambda: self._on_filter_changed("pressure"))

        self._count_label = QLabel("0")
        self._count_label.setStyleSheet(f"""
            color: {BG};
            background: {MUTED};
            border-radius: 8px;
            padding: 1px 7px;
            font-size: 10px;
            font-weight: 700;
        """)
        self._clear_all_btn = QPushButton("清空全部")
        self._clear_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_all_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED};
                background: transparent;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                color: {TEXT};
                border-color: #58a6ff;
                background: {BG3};
            }}
            QPushButton:pressed {{
                background: #264f78;
                border-color: #58a6ff;
            }}
        """)
        self._clear_all_btn.clicked.connect(
            lambda: self.clear_all_requested.emit(self._filter_mode)
        )
        header.addWidget(title)
        header.addWidget(self._stutter_filter_btn)
        header.addWidget(self._minor_filter_btn)
        header.addWidget(self._pressure_filter_btn)
        header.addWidget(self._count_label)
        header.addStretch()
        header.addWidget(self._clear_all_btn)
        layout.addLayout(header)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._scroll_bar = scroll.verticalScrollBar()
        self._scroll_bar.rangeChanged.connect(self._maybe_request_more_history)
        self._scroll_bar.valueChanged.connect(self._maybe_request_more_history)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch()

        scroll.setWidget(self._list_widget)
        layout.addWidget(scroll, stretch=1)

        # Empty state
        self._empty_label = QLabel("暂时还没有卡顿事件。\n正在持续监控你的系统…")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self._list_layout.insertWidget(0, self._empty_label)

    def add_event(self, event: LagEvent):
        """Prepend a new event row at the top of the list."""
        if not self._matches_filter(event):
            return
        if self._empty_label.isVisible():
            self._empty_label.hide()

        row = EventRow(event)
        row.clicked.connect(self._on_row_clicked)
        row.delete_requested.connect(self.event_delete_requested.emit)
        self._rows.insert(0, row)
        self._list_layout.insertWidget(0, row)
        self._total_count += 1
        self._refresh_state()

    def append_history(self, events: list[LagEvent]):
        """Append one older history page, keeping newest rows at the top."""
        self._loading_more = False
        for event in events:
            if not self._matches_filter(event):
                continue
            self._rows.append(EventRow(event))
            row = self._rows[-1]
            row.clicked.connect(self._on_row_clicked)
            row.delete_requested.connect(self.event_delete_requested.emit)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
        self._refresh_state()

    def set_total_count(self, count: int):
        self._total_count = max(0, int(count), len(self._rows))
        self._refresh_state()

    def upsert_event(self, event: LagEvent):
        for row in self._rows:
            if row._lag_event is event or (event.id and row._lag_event.id == event.id):
                if not self._matches_filter(event):
                    # The analysis finalised this event into a different tab
                    # (e.g. compat CPU pressure was reclassified as pressure).
                    self.remove_event(event)
                    return
                row._lag_event = event
                row.refresh()
                return
        self.add_event(event)

    def remove_event(self, event: LagEvent) -> bool:
        for row in list(self._rows):
            if row._lag_event is event or row._lag_event.id == event.id:
                if self._selected_row is row:
                    self._selected_row = None
                self._rows.remove(row)
                self._total_count = max(0, self._total_count - 1)
                row.setParent(None)
                row.deleteLater()
                self._refresh_state()
                return True
        return False

    def clear_events(self):
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._total_count = 0
        self._loading_more = False
        self._selected_row = None
        self._refresh_state()

    def set_filter_mode(self, mode: str):
        if mode not in {"stutter", "minor", "pressure"} or mode == self._filter_mode:
            return
        self._filter_mode = mode
        self._stutter_filter_btn.setChecked(mode == "stutter")
        self._minor_filter_btn.setChecked(mode == "minor")
        self._pressure_filter_btn.setChecked(mode == "pressure")
        self.filter_changed.emit(mode)

    @property
    def filter_mode(self) -> str:
        return self._filter_mode

    def _matches_filter(self, event: LagEvent) -> bool:
        source = event.detection_source
        if not source:
            # Legacy events saved before detection_source existed; infer from
            # the category so old records still land in the correct tab.
            if event.category in ("RESOURCE_PRESSURE_RISK", "CPU Pressure Stall", "I/O Pressure Stall"):
                source = "pressure"
            elif event.category in {"FRAME_SPIKE", "CPU_STAGE_STALL", "TRANSIENT_DISTURBANCE", "LOCAL_STUTTER", "UNDETERMINED"}:
                source = "minor"
            else:
                source = "frame"
        if self._filter_mode == "pressure":
            return source in ("pressure", "system", "compat_pressure")
        if self._filter_mode == "minor":
            return source == "minor"
        return source in ("frame", "compat")

    def _on_filter_changed(self, mode: str):
        self.set_filter_mode(mode)

    def _on_row_clicked(self, event: LagEvent):
        # Deselect previous
        if self._selected_row:
            self._selected_row.set_selected(False)
        # Find and select clicked row
        for row in self._rows:
            if row._lag_event is event or row._lag_event.id == event.id:
                row.set_selected(True)
                self._selected_row = row
                break
        self.event_selected.emit(event)

    def select_event_by_id(self, event_id: int) -> LagEvent | None:
        if not event_id:
            return None
        if self._selected_row:
            self._selected_row.set_selected(False)
            self._selected_row = None
        for row in self._rows:
            if row._lag_event.id == event_id:
                row.set_selected(True)
                self._selected_row = row
                return row._lag_event
        return None

    def _refresh_state(self):
        self._count_label.setText(str(self._total_count))
        has_rows = self._total_count > 0
        self._empty_label.setVisible(not has_rows)
        if self._filter_mode == "pressure":
            self._empty_label.setText("暂时没有系统压力事件。\n系统资源压力较高时会在此提示。")
        elif self._filter_mode == "minor":
            self._empty_label.setText("暂时没有轻微干扰事件。\n短暂抖动或瞬时干扰会在此提示。")
        else:
            self._empty_label.setText("暂时还没有卡顿事件。\n正在持续监控你的系统…")
        self._clear_all_btn.setEnabled(self._total_count > 0)

    def replace_events(self, events: list[LagEvent], total_count: int | None = None):
        self.setUpdatesEnabled(False)
        try:
            new_rows: list[EventRow] = []
            for event in events:
                if not self._matches_filter(event):
                    continue
                row = EventRow(event)
                row.clicked.connect(self._on_row_clicked)
                row.delete_requested.connect(self.event_delete_requested.emit)
                new_rows.append(row)
            self.clear_events()
            self._rows.extend(new_rows)
            for row in new_rows:
                self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            if total_count is not None:
                self._total_count = max(0, int(total_count), len(self._rows))
            self._refresh_state()
        finally:
            self.setUpdatesEnabled(True)

    def _maybe_request_more_history(self):
        if self._loading_more or len(self._rows) >= self._total_count:
            return
        bar = self._scroll_bar
        if bar.maximum() - bar.value() <= 120:
            self._loading_more = True
            self.more_history_requested.emit()
