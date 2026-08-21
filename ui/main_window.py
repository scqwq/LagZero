"""
ui/main_window.py — Main application window.

Layout:
  ┌──────────────────────────────────────────────────────┐
  │  Header: app name + status indicator                 │
  ├──────────────────────────────────────────────────────┤
  │  Live metrics bar (CPU / RAM / Responsiveness)       │
  ├────────────────────┬─────────────────────────────────┤
  │  Event log         │  Detail panel                  │
  │  (scrollable list) │  (shown when event selected)   │
  └────────────────────┴─────────────────────────────────┘
"""
from datetime import datetime
from time import monotonic

import threading

from PySide6.QtCore import Qt, QTimer, Slot, Signal
from PySide6.QtGui import QFont, QColor, QPalette
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSplitter, QFrame, QProgressBar, QStatusBar,
    QSystemTrayIcon, QMenu, QApplication, QComboBox,
    QLineEdit, QPushButton, QCheckBox,
)
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QStyle

from core.models import (
    SystemSample, LagScore, LagEvent, LagSnapshot,
    CompatibilityMetricsSnapshot, GameSessionInfo, GameWindowCandidate, FrameMetricsSnapshot, FrameStutterEpisode,
)
from ui.event_log import EventLogWidget
from ui.detail_panel import DetailPanelWidget


# ---------------------------------------------------------------------------
# Colour constants
# ---------------------------------------------------------------------------

GREEN = "#2ecc71"
AMBER = "#f39c12"
RED   = "#e74c3c"
BG    = "#0d1117"
BG2   = "#161b22"
TEXT  = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
COMPAT_RECOVERY_METRICS_REQUIRED = 6
AUTO_HIGH_PRECISION_RECOVERY_INTERVAL_MS = 20000
AUTO_HIGH_PRECISION_RECOVERY_COOLDOWN_S = 30.0
MANUAL_HIGH_PRECISION_RECOVERY_DURATION_S = 0
AUTO_HIGH_PRECISION_RECOVERY_DURATION_S = 0

EVENT_LABELS = {
    "COMPAT_WINDOW_HANG": "Window Not Responding",
    "COMPAT_VISUAL_FREEZE": "Visual Freeze",
    "COMPAT_STALL": "Responsiveness Stall",
    "COMPAT_CPU_PRESSURE": "CPU Pressure Stall",
    "COMPAT_IO_PRESSURE": "I/O Pressure Stall",
}


def severity_colour(composite: float) -> str:
    if composite < 0.4:
        return GREEN
    if composite < 0.7:
        return AMBER
    return RED


# ---------------------------------------------------------------------------
# Live metric widget
# ---------------------------------------------------------------------------

class MetricBar(QFrame):
    """One labelled metric with a coloured progress bar."""

    def __init__(self, label: str, unit: str = "%", parent=None):
        super().__init__(parent)
        self.unit = unit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        self._label = QLabel(label)
        self._label.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 600;")

        self._value = QLabel("—")
        self._value.setStyleSheet(f"color: {TEXT}; font-size: 22px; font-weight: 700;")

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)
        self._bar.setStyleSheet(f"""
            QProgressBar {{ background: #30363d; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
        """)

        layout.addWidget(self._label)
        layout.addWidget(self._value)
        layout.addWidget(self._bar)

        self.setStyleSheet(f"background: {BG2}; border-radius: 8px;")

    def update_value(self, value: float, colour: str | None = None):
        display = f"{value:.0f}{self.unit}" if self.unit != "ms" else f"{value:.1f} ms"
        self._value.setText(display)
        pct = min(int(value), 100)
        self._bar.setValue(pct)
        c = colour or ACCENT
        self._bar.setStyleSheet(f"""
            QProgressBar {{ background: #30363d; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {c}; border-radius: 2px; }}
        """)
        self._value.setStyleSheet(f"color: {c}; font-size: 22px; font-weight: 700;")


# ---------------------------------------------------------------------------
# Status indicator dot
# ---------------------------------------------------------------------------

class StatusDot(QLabel):
    def __init__(self, parent=None):
        super().__init__("●  监控中", parent)
        self.set_ok()

    def set_ok(self):
        self.setStyleSheet(f"color: {GREEN}; font-size: 12px; font-weight: 700; letter-spacing: 1px;")
        self.setText("●  监控中")

    def set_warning(self):
        self.setStyleSheet(f"color: {AMBER}; font-size: 12px; font-weight: 700; letter-spacing: 1px;")
        self.setText("●  负载升高")

    def set_lag(self):
        self.setStyleSheet(f"color: {RED}; font-size: 12px; font-weight: 700; letter-spacing: 1px;")
        self.setText("●  检测到卡顿")

    def set_learning(self):
        self.setStyleSheet(f"color: {MUTED}; font-size: 12px; font-weight: 700; letter-spacing: 1px;")
        self.setText("●  正在学习基线…")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    # Carries a CleanupReport, not a bare count: "0 cleaned" needs to read very
    # differently depending on whether nothing was found or nothing was allowed.
    cleanup_completed = Signal(object)
    probe_completed = Signal(str)
    recovery_completed = Signal(object)
    frame_detector_reset_requested = Signal()
    snapshot_loaded = Signal(object, object)

    def __init__(
        self,
        collector,
        engine,
        recorder,
        analyzer,
        storage,
        session_detector=None,
        presentmon=None,
        frame_detector=None,
        compat_capture=None,
        compat_detector=None,
        parent=None,
    ):
        super().__init__(parent)
        self._collector = collector
        self._engine = engine
        self._recorder = recorder
        self._analyzer = analyzer
        self._storage = storage
        self._session_detector = session_detector
        self._presentmon = presentmon
        self._frame_detector = frame_detector
        self._compat_capture = compat_capture
        self._compat_detector = compat_detector

        self._active_event: LagEvent | None = None
        self._pending_frame_event: LagEvent | None = None
        self._window_candidates: list[GameWindowCandidate] = []
        self._window_candidates_key: tuple[tuple[int, int, str, int, int], ...] | None = None
        self._deferred_candidates: list[GameWindowCandidate] | None = None
        self._auto_attach = True
        self._last_system_sample: SystemSample | None = None
        self._allow_exit = False
        self._button_feedback_timers: dict[QPushButton, QTimer] = {}
        self._cleanup_in_progress = False
        self._probe_in_progress = False
        self._recovery_in_progress = False
        self._pending_frame_metrics: FrameMetricsSnapshot | None = None
        self._pending_compat_metrics: CompatibilityMetricsSnapshot | None = None
        self._snapshot_cache: dict[int, LagSnapshot | None] = {}
        self._selected_event_id: int | None = None
        self._selected_event_ref: LagEvent | None = None
        self._last_metrics_ui_ts = 0.0
        self._last_statusbar_text = ""
        self._last_statusbar_ts = 0.0
        self._last_pm_status_text = ""
        self._last_pm_status_ts = 0.0
        self._capture_mode = "High Precision"
        self._compat_active = False
        self._compat_recovery_count = 0
        self._last_compat_debug = "compat_debug=inactive"
        self._last_compat_diag_text = ""
        self._last_compat_diag_ts = 0.0
        self._last_capture_diag_text = ""
        self._last_capture_diag_ts = 0.0
        self._manual_candidate_hold_until = 0.0
        self._last_high_precision_recovery_ts = 0.0

        self.setWindowTitle("LagLense")
        self.resize(1100, 720)
        self.setMinimumSize(800, 550)
        self._apply_dark_theme()
        self._build_ui()
        self._connect_signals()
        self._setup_tray()
        QTimer.singleShot(0, self._load_history)
        self._metrics_timer = QTimer(self)
        self._metrics_timer.timeout.connect(self._flush_frame_metrics)
        self._metrics_timer.start(250)
        self._candidate_debounce_timer = QTimer(self)
        self._candidate_debounce_timer.setSingleShot(True)
        self._candidate_debounce_timer.timeout.connect(self._apply_deferred_candidates)
        self._auto_recover_timer = QTimer(self)
        self._auto_recover_timer.timeout.connect(self._maybe_auto_recover_high_precision)
        self._auto_recover_timer.start(AUTO_HIGH_PRECISION_RECOVERY_INTERVAL_MS)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # --- Header ---
        header = QHBoxLayout()
        title = QLabel("LagLense")
        title.setStyleSheet(f"color: {TEXT}; font-size: 20px; font-weight: 800;")
        self._status_dot = StatusDot()
        self._baseline_label = QLabel("基线：学习中…")
        self._baseline_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        header.addWidget(title)
        header.addStretch()
        self._report_lang_combo = QComboBox()
        self._report_lang_combo.addItem("报告：中文", "zh")
        self._report_lang_combo.addItem("Report: English", "en")
        self._report_lang_combo.setCurrentIndex(0)
        self._report_lang_combo.setMinimumWidth(130)
        header.addWidget(self._baseline_label)
        header.addSpacing(10)
        header.addWidget(self._report_lang_combo)
        header.addSpacing(16)
        header.addWidget(self._status_dot)
        root.addLayout(header)

        # --- Capture controls ---
        capture_card = QFrame()
        capture_card.setStyleSheet(f"background: {BG2}; border-radius: 10px;")
        capture_layout = QVBoxLayout(capture_card)
        capture_layout.setContentsMargins(12, 10, 12, 10)
        capture_layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        self._window_label = QLabel("前台窗口：未检测到游戏")
        self._window_label.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
        self._capture_mode_label = QLabel("采集模式：高精度")
        self._capture_mode_label.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: 700;")
        self._pm_status_label = QLabel("PresentMon：空闲")
        self._pm_status_label.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
        self._capture_identity_label = QLabel("采集目标：未设置")
        self._capture_identity_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        self._capture_diag_label = QLabel("诊断：等待采集启动")
        self._capture_diag_label.setWordWrap(True)
        self._capture_diag_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self._probe_result_label = QLabel("探测：尚未运行")
        self._probe_result_label.setWordWrap(True)
        self._probe_result_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        top_row.addWidget(self._window_label, stretch=1)
        top_row.addWidget(self._capture_mode_label)
        top_row.addWidget(self._pm_status_label, stretch=1)
        capture_layout.addLayout(top_row)
        capture_layout.addWidget(self._capture_identity_label)
        capture_layout.addWidget(self._capture_diag_label)
        capture_layout.addWidget(self._probe_result_label)

        control_row = QHBoxLayout()
        control_row.setSpacing(8)
        self._auto_checkbox = QCheckBox("自动附着前台游戏")
        self._auto_checkbox.setChecked(True)
        self._candidate_combo = QComboBox()
        self._candidate_combo.setMinimumWidth(260)
        self._candidate_combo.addItem("没有候选窗口")
        self._candidate_combo.setEnabled(False)
        self._target_input = QLineEdit()
        self._target_input.setPlaceholderText("例如：java.exe / cs2.exe")
        self._apply_target_btn = QPushButton("应用目标")
        self._clean_sessions_btn = QPushButton("清理残留会话")
        self._probe_btn = QPushButton("探测 Present")
        self._recover_btn = QPushButton("恢复高精度")
        control_row.addWidget(self._auto_checkbox)
        control_row.addWidget(self._candidate_combo, stretch=1)
        control_row.addWidget(self._target_input)
        control_row.addWidget(self._apply_target_btn)
        control_row.addWidget(self._clean_sessions_btn)
        control_row.addWidget(self._probe_btn)
        control_row.addWidget(self._recover_btn)
        capture_layout.addLayout(control_row)

        telemetry_row = QHBoxLayout()
        telemetry_row.setSpacing(10)
        self._fps_title, self._fps_value = self._build_capture_chip(telemetry_row, "FPS")
        self._ft_title, self._ft_value = self._build_capture_chip(telemetry_row, "平均帧时")
        self._p95_title, self._p95_value = self._build_capture_chip(telemetry_row, "P95 帧时")
        self._wait_title, self._wait_value = self._build_capture_chip(telemetry_row, "CPU 等待")
        self._set_capture_metrics_unavailable("无数据")
        root.addWidget(capture_card)
        capture_layout.addLayout(telemetry_row)

        # --- Metrics bar ---
        metrics_row = QHBoxLayout()
        self._cpu_bar = MetricBar("CPU")
        self._ram_bar = MetricBar("RAM")
        self._resp_bar = MetricBar("响应延迟", unit="ms")
        self._score_bar = MetricBar("卡顿评分", unit="%")
        for w in (self._cpu_bar, self._ram_bar, self._resp_bar, self._score_bar):
            metrics_row.addWidget(w)
        root.addLayout(metrics_row)

        # --- Splitter: event log | detail panel ---
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("QSplitter::handle { background: #30363d; }")

        self._event_log = EventLogWidget()
        self._detail_panel = DetailPanelWidget()

        splitter.addWidget(self._event_log)
        splitter.addWidget(self._detail_panel)
        splitter.setSizes([380, 680])
        root.addWidget(splitter, stretch=1)

        # --- Status bar ---
        sb = QStatusBar()
        sb.setStyleSheet(f"color: {MUTED}; font-size: 11px; background: {BG};")
        self._event_count_label = QLabel("已记录 0 个事件")
        sb.addPermanentWidget(self._event_count_label)
        self.setStatusBar(sb)
        self._set_status_message("正在采集系统数据…", force=True)

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self._collector.sample_ready.connect(self._on_sample)
        self._engine.score_updated.connect(self._on_score)
        self._engine.lag_started.connect(self._on_lag_started)
        self._engine.lag_ended.connect(self._on_lag_ended)
        self._engine.baseline_updated.connect(self._on_baseline_updated)
        self._event_log.event_selected.connect(self._on_event_selected)
        self._event_log.event_delete_requested.connect(self._on_event_delete_requested)
        self._event_log.clear_all_requested.connect(self._on_clear_all_events_requested)
        self._report_lang_combo.currentIndexChanged.connect(self._on_report_language_changed)
        self._auto_checkbox.toggled.connect(self._on_auto_toggled)
        self._candidate_combo.currentIndexChanged.connect(self._on_candidate_selected)
        self._apply_target_btn.clicked.connect(self._apply_manual_target)
        self._clean_sessions_btn.clicked.connect(self._clean_stale_sessions)
        self._probe_btn.clicked.connect(self._probe_active_presents)
        self._recover_btn.clicked.connect(self._recover_high_precision)
        self.cleanup_completed.connect(self._on_cleanup_completed)
        self.probe_completed.connect(self._on_probe_completed)
        self.recovery_completed.connect(self._on_recovery_completed)
        self.snapshot_loaded.connect(self._on_snapshot_loaded)
        if self._session_detector is not None:
            self._session_detector.session_changed.connect(self._on_session_changed)
            self._session_detector.candidates_changed.connect(self._on_candidates_changed)
            self._session_detector.error_occurred.connect(self._on_capture_error)
        if self._presentmon is not None:
            self._presentmon.status_changed.connect(self._on_presentmon_status)
            self._presentmon.metrics_updated.connect(self._on_frame_metrics)
            self._presentmon.target_changed.connect(self._on_requested_target_changed)
            self._presentmon.capture_identity_changed.connect(self._on_capture_identity_changed)
            self._presentmon.diagnostics_changed.connect(self._on_capture_diagnostics_changed)
            self._presentmon.error_occurred.connect(self._on_capture_error)
        if self._compat_capture is not None:
            self._compat_capture.status_changed.connect(self._on_compat_status)
            self._compat_capture.metrics_updated.connect(self._on_compat_metrics)
            self._compat_capture.error_occurred.connect(self._on_capture_error)
            self._compat_capture.mode_changed.connect(self._on_capture_mode_changed)
        detector = getattr(self, "_frame_detector", None)
        if detector is not None:
            detector.stutter_started.connect(self._on_frame_stutter_started)
            detector.stutter_ended.connect(self._on_frame_stutter_ended)
            detector.status_changed.connect(self._on_presentmon_status)
            self.frame_detector_reset_requested.connect(detector.reset_target)
        compat_detector = getattr(self, "_compat_detector", None)
        if compat_detector is not None:
            compat_detector.stutter_started.connect(self._on_frame_stutter_started)
            compat_detector.stutter_ended.connect(self._on_frame_stutter_ended)
            compat_detector.status_changed.connect(self._on_compat_status)
            self.frame_detector_reset_requested.connect(compat_detector.reset_target)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_sample(self, sample: SystemSample):
        self._last_system_sample = sample
        score_obj = self._engine.recent_scores[-1] if self._engine.recent_scores else None
        composite = score_obj.composite if score_obj else 0.0
        self._recorder.record_sample(sample, composite)
        self._engine.ingest(sample)

    @Slot(object)
    def _on_score(self, score: LagScore):
        c_cpu = severity_colour(score.cpu_score)
        c_ram = severity_colour(score.ram_score)
        c_resp = severity_colour(score.responsiveness_score)
        c_composite = severity_colour(score.composite)

        # Get last sample for actual values
        samples = self._engine.recent_samples
        if samples:
            last = samples[-1]
            self._cpu_bar.update_value(last.cpu_percent, c_cpu)
            self._ram_bar.update_value(last.ram_percent, c_ram)
            self._resp_bar.update_value(last.responsiveness_ms, c_resp)

        self._score_bar.update_value(score.composite * 100, c_composite)

        # Update status dot
        if score.composite >= 0.6:
            self._status_dot.set_lag()
        elif score.composite >= 0.35:
            self._status_dot.set_warning()
        else:
            if not self._engine.baseline.is_ready:
                self._status_dot.set_learning()
            else:
                self._status_dot.set_ok()

    @Slot(object)
    def _on_lag_started(self, started_at: datetime):
        self._active_event = LagEvent(
            id=None,
            started_at=started_at,
            ended_at=None,
            peak_composite_score=0.0,
            cause="This stutter is still in progress. The final report will be filled in after the game recovers.",
            cause_code="REPORT_PENDING",
            category="REPORT_PENDING",
            scope="UNDETERMINED",
            is_pending=True,
        )
        self._event_log.upsert_event(self._active_event)
        self._set_status_message(f"⚠  Lag event started at {started_at.strftime('%H:%M:%S')}", force=True)

    @Slot(object, float)
    def _on_lag_ended(self, ended_at: datetime, peak_score: float):
        if self._active_event is None:
            return

        event = self._active_event
        event.ended_at = ended_at
        event.peak_composite_score = peak_score
        event.duration_seconds = (ended_at - event.started_at).total_seconds()

        # Capture snapshot
        snapshot = self._recorder.capture(event)

        # Analyse cause
        category, cause, scope = self._analyzer.analyze(
            snapshot.peak_sample, snapshot.pre_lag_samples
        )
        event.cause = cause
        event.cause_code = category
        event.category = category
        event.scope = scope
        event.is_pending = False

        # Persist
        event_id = self._storage.save_event(event)
        event.id = event_id
        snapshot.event_id = event_id
        self._storage.save_snapshot(snapshot)
        self._snapshot_cache[event_id] = snapshot
        if self._selected_event_ref is event:
            self._selected_event_id = event_id
            self._detail_panel.show_event(event, snapshot)

        # Update UI
        self._event_log.upsert_event(event)
        count = self._storage.event_count()
        self._event_count_label.setText(f"已记录 {count} 个事件")
        self._set_status_message(
            f"✓  卡顿结束 — {round(event.duration_seconds, 1)} 秒 — {category}"
            , force=True
        )

        # Tray notification
        if self._tray and self._tray.isVisible():
            self._tray.showMessage(
                "检测到卡顿事件",
                f"{category}: {cause[:80]}…" if len(cause) > 80 else cause,
                QSystemTrayIcon.MessageIcon.Warning,
                4000,
            )

        self._active_event = None

    @Slot(object)
    def _on_baseline_updated(self, baseline):
        if baseline.is_ready:
            self._baseline_label.setText(
                f"基线：CPU {baseline.cpu_mean:.0f}% ± {baseline.cpu_std:.0f}  |  "
                f"内存 {baseline.ram_mean:.0f}% ± {baseline.ram_std:.0f}"
            )
        else:
            remaining = max(0, 60 - baseline.sample_count)
            self._baseline_label.setText(f"基线：学习中…（还需 {remaining} 秒）")

    @Slot(object)
    def _on_event_selected(self, event: LagEvent):
        self._selected_event_ref = event
        event_id = event.id
        self._selected_event_id = event_id
        if event.is_pending or event_id is None:
            self._detail_panel.show_loading_event(event)
            return
        if event_id and event_id in self._snapshot_cache:
            self._detail_panel.show_event(event, self._snapshot_cache[event_id])
            return
        self._detail_panel.show_loading_event(event)

        def _worker():
            snapshot = self._storage.get_snapshot_for_event(event.id)
            self.snapshot_loaded.emit(event, snapshot)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(object, object)
    def _on_snapshot_loaded(self, event: LagEvent, snapshot: LagSnapshot | None):
        event_id = event.id
        if event_id:
            self._snapshot_cache[event_id] = snapshot
        if event_id != self._selected_event_id:
            return
        self._detail_panel.show_event(event, snapshot)

    @Slot(object)
    def _on_event_delete_requested(self, event: LagEvent):
        if event is self._active_event:
            self._active_event = None
        if event is self._pending_frame_event:
            self._pending_frame_event = None
        deleted = self._storage.delete_event(event.id)
        if event.id and not deleted:
            self._set_status_message("删除卡顿报告失败：记录不存在或已被删除", force=True)
            return
        if event.id:
            self._snapshot_cache.pop(event.id, None)
        if self._selected_event_id == event.id:
            self._selected_event_id = None
            self._selected_event_ref = None
        self._event_log.remove_event(event)
        self._detail_panel.clear_event()
        count = self._storage.event_count()
        self._event_count_label.setText(f"已记录 {count} 个事件")
        self._set_status_message("已删除 1 条卡顿报告", force=True)

    @Slot()
    def _on_clear_all_events_requested(self):
        removed = self._storage.delete_all_events()
        if removed <= 0:
            self._set_status_message("当前没有可清空的卡顿报告", force=True)
            return
        self._snapshot_cache.clear()
        self._selected_event_id = None
        self._selected_event_ref = None
        self._event_log.clear_events()
        self._detail_panel.clear_event()
        self._event_count_label.setText("已记录 0 个事件")
        self._set_status_message(f"已清空 {removed} 条卡顿报告", force=True)

    @Slot(int)
    def _on_report_language_changed(self, _index: int):
        language = self._report_lang_combo.currentData()
        if not language:
            language = "zh"
        self._detail_panel.set_report_language(str(language))

    @Slot(bool)
    def _on_auto_toggled(self, enabled: bool):
        self._auto_attach = enabled
        self._candidate_combo.setEnabled(not enabled)
        if enabled and self._presentmon is not None:
            self._pm_status_label.setText("PresentMon：等待前台游戏")
            self._presentmon.stop_capture()

    @Slot(int)
    def _on_candidate_selected(self, index: int):
        if self._auto_attach or index < 0 or index >= len(self._window_candidates):
            return
        self._manual_candidate_hold_until = monotonic() + 2.0
        self._target_input.setText(self._window_candidates[index].process_name)

    @Slot()
    def _apply_manual_target(self):
        if self._presentmon is None:
            return
        target = self._target_input.text().strip()
        if not target:
            self.statusBar().showMessage("请先输入进程名，例如 java.exe 或 cs2.exe。")
            return
        if self._auto_attach:
            self._auto_checkbox.setChecked(False)
        if self._frame_detector is not None:
            self.frame_detector_reset_requested.emit()
        target, pid = self._resolve_capture_target(target)
        self._manual_candidate_hold_until = monotonic() + 3.0
        self._target_input.setText(target)
        if hasattr(self._collector, "set_tracked_process"):
            self._collector.set_tracked_process(target, pid)
        self._presentmon.set_target(target, pid=pid)
        if self._compat_capture is not None:
            self._compat_capture.set_target(target, pid=pid)
        self._presentmon.start_capture()
        self._flash_button(self._apply_target_btn, f"已应用采集目标：{self._presentmon.target_description()}")

    @Slot()
    def _clean_stale_sessions(self):
        if self._presentmon is None or self._cleanup_in_progress:
            return
        self._cleanup_in_progress = True
        self._clean_sessions_btn.setEnabled(False)
        self._flash_button(self._clean_sessions_btn, "正在清理残留 trace 会话…")

        def _worker():
            self._presentmon.cleanup_stale_sessions()
            self.cleanup_completed.emit(self._presentmon.last_cleanup_report())

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(object)
    def _on_cleanup_completed(self, report):
        self._cleanup_in_progress = False
        self._clean_sessions_btn.setEnabled(True)
        message = self._describe_cleanup(report)
        self._set_status_message(message, force=True)
        self._flash_button(self._clean_sessions_btn, message)

    @staticmethod
    def _describe_cleanup(report) -> str:
        """
        Turn a CleanupReport into text that distinguishes the three outcomes.

        Previously every result rendered as "已清理 N 个残留会话" using the count
        of sessions *found*, so a denied cleanup looked like a successful one and
        the user had no way to learn that elevation was the missing piece.
        """
        if report is None:
            return "清理残留会话：无结果"
        if report.found == 0:
            return "没有发现残留 trace 会话"
        if report.stopped == report.found:
            return f"已清理 {report.stopped} 个残留 trace 会话"
        if report.needs_elevation:
            return (
                f"发现 {report.found} 个残留会话，但停止被拒绝（需要管理员权限）。"
                "请以管理员身份重启 LagLense 后再试。"
            )
        return (
            f"已清理 {report.stopped}/{report.found} 个残留会话，"
            f"{report.failed} 个失败：{report.detail or '原因未知'}"
        )

    @Slot()
    def _probe_active_presents(self):
        if self._presentmon is None or self._probe_in_progress or self._recovery_in_progress:
            return
        self._probe_in_progress = True
        self._set_capture_action_buttons_enabled(False)
        self._probe_result_label.setText("探测：正在执行 3 秒无过滤 Present 扫描…")
        self._flash_button(self._probe_btn, "正在探测哪些进程真正产生 Present…")

        def _worker():
            result = self._presentmon.probe_active_presents(duration_seconds=3)
            self.probe_completed.emit(result)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(str)
    def _on_probe_completed(self, result: str):
        self._probe_in_progress = False
        self._set_capture_action_buttons_enabled(True)
        self._probe_result_label.setText(f"探测：{result}")
        self._set_status_message("Present 探测完成", force=True)
        self._flash_button(self._probe_btn, "Present 探测完成")

    @Slot()
    def _recover_high_precision(self):
        self._start_high_precision_recovery(
            duration_seconds=MANUAL_HIGH_PRECISION_RECOVERY_DURATION_S,
            manual=True,
        )

    def _start_high_precision_recovery(self, *, duration_seconds: int, manual: bool):
        if self._presentmon is None or self._probe_in_progress or self._recovery_in_progress:
            return
        target_name, target_pid = self._presentmon.requested_target()
        if not target_name and not target_pid:
            if manual:
                self._set_status_message("请先应用一个采集目标，再尝试恢复高精度。", force=True)
            return

        self._recovery_in_progress = True
        self._last_high_precision_recovery_ts = monotonic()
        self._set_capture_action_buttons_enabled(False)
        failure_reason = self._presentmon.last_failure_reason().lower()
        if manual:
            if "1450" in failure_reason:
                self._probe_result_label.setText("恢复：检测到 ETW 会话受阻，正在清理残留并直接重启高精度捕获…")
            else:
                self._probe_result_label.setText("恢复：正在直接重启高精度捕获，避免额外 probe 会话…")
            self._flash_button(self._recover_btn, "正在尝试恢复高精度采集…")
        else:
            self._set_capture_diag_text(
                "诊断：兼容模式下正在低频直接重启高精度采集，避免额外 trace probe…",
                min_interval=0.35,
            )

        def _worker():
            result = self._presentmon.recover_high_precision_target(duration_seconds=duration_seconds)
            self.recovery_completed.emit({"manual": manual, "result": result})

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(object)
    def _on_recovery_completed(self, payload: object):
        info = payload if isinstance(payload, dict) else {"manual": True, "result": str(payload or "")}
        manual = bool(info.get("manual"))
        result = str(info.get("result") or "")
        self._recovery_in_progress = False
        self._set_capture_action_buttons_enabled(True)

        prefix = "恢复" if manual else "自动恢复"
        self._probe_result_label.setText(f"{prefix}：{result}")
        lowered = result.lower()
        success = "waiting for frame data" in lowered or "restarted high-precision capture" in lowered
        if success:
            self._set_status_message("高精度恢复流程已重启，正在等待新的帧数据", force=True)
        elif "1450" in lowered:
            self._set_status_message("高精度恢复受阻：ETW trace 会话仍然启动失败", force=True)
        elif manual:
            self._set_status_message("高精度恢复流程完成，但高精度采集仍未成功启动", force=True)

        if manual:
            button_text = "高精度恢复已重启" if success else "高精度恢复受阻，已保留兼容模式"
            if "1450" in lowered:
                button_text = "ETW 会话仍受阻，已保留兼容模式"
            self._flash_button(
                self._recover_btn,
                button_text,
            )

    @Slot(object)
    def _on_session_changed(self, session: GameSessionInfo):
        if session.is_valid:
            self._window_label.setText(
                f"前台窗口：{session.process_name} — {session.window_title[:70]}"
            )
            if hasattr(self._collector, "set_tracked_process"):
                self._collector.set_tracked_process(session.process_name, session.pid)
            if self._auto_attach and self._presentmon is not None:
                self._target_input.setText(session.process_name)
                if self._frame_detector is not None:
                    self.frame_detector_reset_requested.emit()
                changed = self._presentmon.set_target(session.process_name, pid=session.pid)
                if self._compat_capture is not None:
                    self._compat_capture.set_target(session.process_name, pid=session.pid)
                if changed or self._presentmon._process.state() == self._presentmon._process.ProcessState.NotRunning:
                    self._presentmon.start_capture()
        else:
            target_desc = ""
            if self._presentmon is not None:
                target_desc = self._presentmon.target_description()
            if target_desc and target_desc != "No target":
                self._window_label.setText(f"前台窗口：未检测到游戏（当前采集目标 {target_desc}）")
            else:
                self._window_label.setText("前台窗口：未检测到游戏")
            if self._auto_attach and hasattr(self._collector, "set_tracked_process"):
                self._collector.set_tracked_process("", None)

    @Slot(object)
    def _on_candidates_changed(self, candidates: list[GameWindowCandidate]):
        now = monotonic()
        if not self._auto_attach and (
            self._candidate_combo.hasFocus()
            or self._candidate_combo.view().isVisible()
            or now < self._manual_candidate_hold_until
        ):
            self._deferred_candidates = candidates
            self._candidate_debounce_timer.start(600)
            return
        self._apply_candidates(candidates)

    def _apply_candidates(self, candidates: list[GameWindowCandidate]):
        candidates_key = tuple(
            (item.hwnd, item.pid, item.process_name, item.width, item.height)
            for item in candidates
        )
        if candidates_key == self._window_candidates_key:
            return
        self._window_candidates_key = candidates_key
        previous_key = None
        index = self._candidate_combo.currentIndex()
        if 0 <= index < len(self._window_candidates):
            selected = self._window_candidates[index]
            previous_key = (selected.hwnd, selected.pid, selected.process_name)
        self._window_candidates = candidates
        self._candidate_combo.blockSignals(True)
        self._candidate_combo.clear()
        if not candidates:
            self._candidate_combo.addItem("没有候选窗口")
        else:
            restore_index = 0
            for candidate in candidates:
                label = (
                    f"{candidate.process_name} | {candidate.title[:36]} "
                    f"({candidate.width}x{candidate.height})"
                )
                self._candidate_combo.addItem(label)
                current_key = (candidate.hwnd, candidate.pid, candidate.process_name)
                if previous_key and current_key == previous_key:
                    restore_index = self._candidate_combo.count() - 1
            self._candidate_combo.setCurrentIndex(restore_index)
        self._candidate_combo.blockSignals(False)

    @Slot()
    def _apply_deferred_candidates(self):
        if self._deferred_candidates is None:
            return
        now = monotonic()
        if not self._auto_attach and (
            self._candidate_combo.hasFocus()
            or self._candidate_combo.view().isVisible()
            or now < self._manual_candidate_hold_until
        ):
            self._candidate_debounce_timer.start(600)
            return
        candidates = self._deferred_candidates
        self._deferred_candidates = None
        self._apply_candidates(candidates)

    @Slot(str)
    def _on_presentmon_status(self, message: str):
        self._set_pm_status_text(f"PresentMon：{message}")
        lowered = message.lower()
        if (
            "failed" in lowered
            or "not found" in lowered
            or "waiting for target" in lowered
            or "no frame data" in lowered
        ):
            self._set_capture_metrics_unavailable("无数据")
        if any(token in lowered for token in ["1450", "access denied", "capture failed"]):
            self._activate_compatibility_mode(message)
        self._set_status_message(message)

    @Slot()
    def _maybe_auto_recover_high_precision(self):
        if not self._compat_active or self._presentmon is None:
            return
        if self._probe_in_progress or self._recovery_in_progress:
            return
        failure_reason = self._presentmon.last_failure_reason().lower()
        if "access denied" in failure_reason or "1450" in failure_reason:
            return
        target_name, target_pid = self._presentmon.requested_target()
        if not target_name and not target_pid:
            return
        if (monotonic() - self._last_high_precision_recovery_ts) < AUTO_HIGH_PRECISION_RECOVERY_COOLDOWN_S:
            return
        self._start_high_precision_recovery(
            duration_seconds=AUTO_HIGH_PRECISION_RECOVERY_DURATION_S,
            manual=False,
        )

    @Slot(object)
    def _on_frame_metrics(self, metrics: FrameMetricsSnapshot):
        if self._compat_active:
            self._compat_recovery_count += 1
            if self._compat_recovery_count >= COMPAT_RECOVERY_METRICS_REQUIRED:
                self._deactivate_compatibility_mode("High precision capture recovered")
            else:
                return
        else:
            self._compat_recovery_count = 0
        self._pending_frame_metrics = metrics
        now = monotonic()
        if (now - self._last_metrics_ui_ts) >= 0.25:
            self._flush_frame_metrics()

    @Slot(str)
    def _on_compat_status(self, message: str):
        if self._compat_active:
            self._set_pm_status_text(f"兼容模式：{message}")
        self._set_status_message(message)

    @Slot(object)
    def _on_compat_metrics(self, metrics: CompatibilityMetricsSnapshot):
        if not self._compat_active:
            return
        self._last_compat_debug = (
            f"compat_debug=response_ms={metrics.response_time_ms:.1f} | hung={metrics.is_hung} | "
            f"visual={'steady' if metrics.visual_change_ratio == 0.0 else 'changed'} | "
            f"frozen_streak={metrics.visual_frozen_streak} | "
            f"cpu={metrics.process_cpu_percent:.1f}% | mem={metrics.process_memory_mb:.0f}MB | "
            f"io={(metrics.process_read_kb_s + metrics.process_write_kb_s):.0f}KB/s | threads={metrics.thread_count}"
        )
        diag_text = "诊断：兼容模式运行中\n" + self._last_compat_debug
        self._last_compat_diag_text = diag_text
        self._set_capture_diag_text(diag_text, min_interval=0.35)
        self._pending_compat_metrics = metrics
        now = monotonic()
        if (now - self._last_metrics_ui_ts) >= 0.25:
            self._flush_frame_metrics()

    @Slot(str)
    def _on_capture_mode_changed(self, mode: str):
        self._capture_mode = mode
        self._capture_mode_label.setText(
            f"采集模式：{'兼容模式' if mode == 'Compatibility' else '高精度'}"
        )

    @Slot(str)
    def _on_requested_target_changed(self, description: str):
        self._capture_identity_label.setText(f"采集目标：{description}")
        if self._presentmon is None:
            return
        target, pid = self._presentmon.requested_target()
        if target:
            self._target_input.setText(target)
        if hasattr(self._collector, "set_tracked_process"):
            self._collector.set_tracked_process(target, pid)
        if self._compat_capture is not None:
            self._compat_capture.set_target(target, pid=pid)
        if self._frame_detector is not None:
            self.frame_detector_reset_requested.emit()

    @Slot(str)
    def _on_capture_identity_changed(self, description: str):
        self._capture_identity_label.setText(f"采集状态：{description}")

    @Slot(str)
    def _on_capture_diagnostics_changed(self, diagnostics: str):
        if self._compat_active:
            return
        self._set_capture_diag_text(f"诊断：{diagnostics}", min_interval=0.75)

    @Slot(object)
    def _on_frame_stutter_started(self, started_at: datetime):
        self._pending_frame_event = LagEvent(
            id=None,
            started_at=started_at,
            ended_at=None,
            peak_composite_score=0.0,
            cause="This stutter is still in progress. The final report will be filled in after the game recovers.",
            cause_code="REPORT_PENDING",
            category="REPORT_PENDING",
            scope="UNDETERMINED",
            is_pending=True,
        )
        self._event_log.upsert_event(self._pending_frame_event)
        self.statusBar().showMessage(f"帧时间卡顿开始于 {started_at.strftime('%H:%M:%S')}")

    @Slot(object)
    def _on_frame_stutter_ended(self, episode: FrameStutterEpisode):
        event = self._pending_frame_event or LagEvent(
            id=None,
            started_at=episode.started_at,
            ended_at=episode.ended_at,
            peak_composite_score=episode.severity,
            cause="",
            cause_code="",
        )
        event.started_at = episode.started_at
        event.ended_at = episode.ended_at
        event.peak_composite_score = episode.severity
        event.duration_seconds = (episode.ended_at - episode.started_at).total_seconds()
        event.is_pending = False

        # Capture first, then analyse: the snapshot is what gives the analyzer the
        # process/RAM/VRAM context. Frame events used to skip the analyzer entirely
        # and report only frame timings, even though this snapshot was already
        # being recorded and persisted.
        snapshot = self._recorder.capture(event)
        # An empty pre-lag buffer means the recorder never saw a real sample, so
        # snapshot.peak_sample is its zero-filled placeholder. Feeding that to the
        # rules would manufacture a verdict out of nothing.
        peak_sample = snapshot.peak_sample if snapshot.pre_lag_samples else None
        if not episode.category:
            episode.category = self._classify_frame_category(episode)
        verdict = self._analyzer.analyze_frame_episode(
            episode, peak_sample, snapshot.pre_lag_samples
        )
        event.cause = verdict.explanation
        event.category = verdict.category
        event.cause_code = verdict.category
        event.scope = verdict.scope
        event.frame_summary = verdict.frame_summary

        event_id = self._storage.save_event(event)
        event.id = event_id
        # Reuse the snapshot captured above rather than calling capture() again:
        # the first call reset the peak tracker, so a second call would return a
        # different, emptier snapshot than the one the verdict was derived from.
        snapshot.event_id = event_id
        self._storage.save_snapshot(snapshot)
        self._snapshot_cache[event_id] = snapshot
        if self._selected_event_ref is event:
            self._selected_event_id = event_id
            self._detail_panel.show_event(event, snapshot)

        self._event_log.upsert_event(event)
        count = self._storage.event_count()
        self._event_count_label.setText(f"已记录 {count} 个事件")
        self.statusBar().showMessage(
            f"已记录 {event.category} — 峰值 {episode.peak_frame_time_ms:.1f} ms"
        )
        self._pending_frame_event = None

    @Slot(str)
    def _on_capture_error(self, message: str):
        self._pm_status_label.setText(f"PresentMon：{message[:90]}")
        self._set_capture_metrics_unavailable("错误")
        lowered = message.lower()
        if any(token in lowered for token in ["1450", "access denied", "no frame data", "failed"]):
            self._activate_compatibility_mode(message)
        self._set_status_message(message, force=True)

    # ------------------------------------------------------------------
    # History load
    # ------------------------------------------------------------------

    def _load_history(self):
        events = self._storage.get_recent_events(limit=120)
        for e in reversed(events):
            self._event_log.add_event(e)
        count = self._storage.event_count()
        self._event_count_label.setText(f"已记录 {count} 个事件")

    @Slot()
    def _flush_frame_metrics(self):
        self._last_metrics_ui_ts = monotonic()
        if self._compat_active:
            metrics = self._pending_compat_metrics
            if metrics is None:
                return
            self._pending_compat_metrics = None
            self._fps_value.setText("兼容")
            self._ft_value.setText(f"{metrics.response_time_ms:.1f} ms")
            self._p95_value.setText(f"{metrics.process_cpu_percent:.0f}% CPU")
            io_kb_s = metrics.process_read_kb_s + metrics.process_write_kb_s
            self._wait_value.setText("未响应" if metrics.is_hung else f"{io_kb_s:.0f} KB/s")
            return

        metrics = self._pending_frame_metrics
        if metrics is None:
            return
        self._pending_frame_metrics = None
        self._fps_value.setText(f"{metrics.fps:.0f}")
        self._ft_value.setText(f"{metrics.avg_frame_time_ms:.1f} ms")
        self._p95_value.setText(f"{metrics.p95_frame_time_ms:.1f} ms")
        self._wait_value.setText(f"{metrics.cpu_wait_ms:.1f} ms")

    def _set_status_message(self, message: str, force: bool = False):
        text = (message or "").strip()
        if not text:
            return
        now = monotonic()
        if not force and text == self._last_statusbar_text and (now - self._last_statusbar_ts) < 0.75:
            return
        self._last_statusbar_text = text
        self._last_statusbar_ts = now
        self.statusBar().showMessage(text)

    def _set_pm_status_text(self, text: str, force: bool = False):
        value = (text or "").strip()
        if not value:
            return
        now = monotonic()
        if not force and value == self._last_pm_status_text and (now - self._last_pm_status_ts) < 1.0:
            return
        if not force and (now - self._last_pm_status_ts) < 0.35:
            return
        self._last_pm_status_text = value
        self._last_pm_status_ts = now
        self._pm_status_label.setText(value)

    def _set_capture_diag_text(self, text: str, *, force: bool = False, min_interval: float = 0.75):
        value = (text or "").strip()
        if not value:
            return
        now = monotonic()
        if not force and value == self._last_capture_diag_text and (now - self._last_capture_diag_ts) < 1.0:
            return
        if not force and (now - self._last_capture_diag_ts) < min_interval:
            return
        self._last_capture_diag_text = value
        self._last_capture_diag_ts = now
        self._capture_diag_label.setText(value)

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self._tray.setIcon(icon)
        self.setWindowIcon(icon)
        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.show()

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_dark_theme(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background: {BG};
                color: {TEXT};
                font-family: 'Segoe UI', 'SF Pro Display', system-ui, sans-serif;
                font-size: 13px;
            }}
            QSplitter {{ background: {BG}; }}
            QScrollBar:vertical {{
                background: {BG2}; width: 8px; border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: #30363d; border-radius: 4px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QLineEdit, QComboBox, QPushButton {{
                background: #11161d;
                color: {TEXT};
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 6px 8px;
            }}
            QPushButton[active='true'] {{
                background: {ACCENT};
                color: #0d1117;
                border: 1px solid {ACCENT};
                font-weight: 700;
            }}
            QCheckBox {{
                color: {TEXT};
            }}
        """)

    def _build_capture_chip(self, parent_layout: QHBoxLayout, label: str) -> tuple[QLabel, QLabel]:
        chip = QFrame()
        chip.setStyleSheet("background: #11161d; border-radius: 8px;")
        layout = QVBoxLayout(chip)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        title = QLabel(label)
        title.setStyleSheet(f"color: {MUTED}; font-size: 10px; font-weight: 700;")
        value = QLabel("—")
        value.setStyleSheet(f"color: {ACCENT}; font-size: 18px; font-weight: 800;")
        layout.addWidget(title)
        layout.addWidget(value)
        parent_layout.addWidget(chip)
        return title, value

    def _set_capture_metrics_unavailable(self, text: str):
        self._fps_value.setText(text)
        self._ft_value.setText(text)
        self._p95_value.setText(text)
        self._wait_value.setText(text)

    def _set_capture_action_buttons_enabled(self, enabled: bool):
        self._probe_btn.setEnabled(enabled)
        self._recover_btn.setEnabled(enabled)

    def _activate_compatibility_mode(self, reason: str):
        if self._compat_capture is None or self._compat_active:
            return
        self._compat_active = True
        self._compat_recovery_count = 0
        self._capture_mode = "Compatibility"
        self._capture_mode_label.setText("采集模式：兼容模式")
        self._capture_mode_label.setStyleSheet(f"color: {AMBER}; font-size: 12px; font-weight: 700;")
        self._set_capture_chip_labels("模式", "响应延迟", "进程 CPU", "IO / 未响应")
        self._last_compat_diag_text = f"诊断：由于高精度采集不可用，已自动切换到兼容模式。\n最近原因：{reason}"
        self._last_compat_diag_ts = monotonic()
        self._set_capture_diag_text(self._last_compat_diag_text, force=True)
        self._set_pm_status_text("兼容模式：正在运行", force=True)
        self._status_dot.set_warning()
        self._compat_capture.start_capture()
        self._set_status_message(f"已切换到兼容模式：{reason}", force=True)

    def _deactivate_compatibility_mode(self, reason: str):
        if self._compat_capture is None or not self._compat_active:
            return
        self._compat_active = False
        self._compat_recovery_count = 0
        self._pending_compat_metrics = None
        self._compat_capture.stop_capture()
        self._capture_mode = "High Precision"
        self._capture_mode_label.setText("采集模式：高精度")
        self._capture_mode_label.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: 700;")
        self._set_capture_chip_labels("FPS", "平均帧时", "P95 帧时", "CPU 等待")
        self._last_compat_debug = "compat_debug=inactive"
        self._last_compat_diag_text = ""
        self._last_compat_diag_ts = 0.0
        self._last_capture_diag_text = ""
        self._last_capture_diag_ts = 0.0
        self._set_pm_status_text("PresentMon：等待恢复高精度采集", force=True)
        self._set_status_message(reason, force=True)

    def _set_capture_chip_labels(self, fps: str, ft: str, p95: str, wait: str):
        self._fps_title.setText(fps)
        self._ft_title.setText(ft)
        self._p95_title.setText(p95)
        self._wait_title.setText(wait)

    def _resolve_capture_target(self, target: str) -> tuple[str, int | None]:
        lowered = target.strip().lower()
        if not lowered:
            return "", None

        selected_pid = None
        selected_name = ""
        index = self._candidate_combo.currentIndex()
        if 0 <= index < len(self._window_candidates):
            candidate = self._window_candidates[index]
            selected_pid = candidate.pid
            selected_name = candidate.process_name
            if candidate.process_name.lower() == lowered:
                return candidate.process_name, candidate.pid

        for candidate in self._window_candidates:
            if candidate.process_name.lower() == lowered:
                return candidate.process_name, candidate.pid

        if not lowered.endswith(".exe"):
            noext = lowered.removesuffix(".exe")
            ranked: list[GameWindowCandidate] = []
            for candidate in self._window_candidates:
                candidate_name = candidate.process_name.lower()
                candidate_noext = candidate_name.removesuffix(".exe")
                if candidate_noext == noext:
                    ranked.append(candidate)
                    continue
                if candidate_noext.startswith(noext) or noext in candidate_noext:
                    ranked.append(candidate)
            unique = {item.process_name.lower(): item for item in ranked}
            if len(unique) == 1:
                match = next(iter(unique.values()))
                self._set_status_message(
                    f"Resolved '{target}' to '{match.process_name}' for PresentMon matching.",
                    force=True,
                )
                return match.process_name, match.pid

        if selected_pid and selected_name and lowered in {
            selected_name.lower(),
            selected_name.lower().removesuffix(".exe"),
        }:
            return selected_name, selected_pid
        return target, None

    @staticmethod
    def _classify_frame_category(episode: FrameStutterEpisode) -> str:
        """
        Frame-side guess at the bottleneck, used only as a fallback.

        This is deliberately the weaker input: CauseAnalyzer prefers a concrete
        system verdict and only falls back to this when the system rules land on
        UNDETERMINED / LOCAL_STUTTER. Compatibility mode reports no CPU wait or
        GPU busy at all, so it never reaches here with useful numbers — the
        compat detector sets its own category instead.

        The real work now happens in core.frame_attribution, which splits the
        worst frame's excess time across CPU / GPU / present-path / display using
        each stage's own learned baseline. This method used to re-derive a verdict
        from two fixed thresholds over peak_gpu_busy and peak_cpu_wait — numbers
        the old v1 mapping had transposed, and that meant nothing without knowing
        what the game normally runs at.
        """
        attribution = episode.attribution
        # A low-confidence verdict deliberately does NOT win here: it has to stay
        # weak so CauseAnalyzer's system rules can outrank it, instead of a
        # 0.3-confidence guess being printed as the cause.
        if attribution is not None and attribution.category and attribution.is_confident:
            return attribution.category
        if episode.dropped_frame_count:
            return "DISPLAY_PIPELINE"
        return "LOCAL_STUTTER"

    def _flash_button(self, button: QPushButton, status_text: str):
        button.setProperty("active", True)
        button.style().unpolish(button)
        button.style().polish(button)
        self._set_status_message(status_text, force=True)

        timer = self._button_feedback_timers.get(button)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda b=button: self._clear_button_flash(b))
            self._button_feedback_timers[button] = timer
        timer.start(1200)

    def _clear_button_flash(self, button: QPushButton):
        button.setProperty("active", False)
        button.style().unpolish(button)
        button.style().polish(button)

    def closeEvent(self, event):
        """Minimise to tray instead of quitting."""
        if self._allow_exit:
            event.accept()
            return
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "System Lag Detective",
            "Still running in the background.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def quit_application(self):
        self._allow_exit = True
        self._tray.hide()
        QApplication.quit()
