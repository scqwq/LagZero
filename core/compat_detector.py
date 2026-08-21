"""
core/compat_detector.py — Compatibility-mode stutter detector.

Turns window responsiveness and process-pressure samples into lag episodes when
high-precision frame telemetry is unavailable.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QObject, Signal, Slot

from core.collectors import per_core_to_machine_share
from core.models import CompatibilitySample, FrameStutterEpisode


RESPONSE_SPIKE_MS = 120.0
RESPONSE_FREEZE_MS = 250.0
CALM_TIME_TO_END_MS = 900.0
VISUAL_FROZEN_STREAK_LIMIT = 8
# psutil's per-process cpu_percent counts 100% per CORE, so a 32-thread
# machine shows the target game at 300–2000% while perfectly healthy. The old
# ≥85 (per-core) rule fired constantly on such machines; the share of the
# whole machine is the number that actually means "the game is starving".
CPU_PRESSURE_MACHINE_SHARE = 70.0   # % of whole machine


class CompatibilityStutterDetector(QObject):
    stutter_started = Signal(object)
    stutter_ended = Signal(object)
    status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recent: deque[CompatibilitySample] = deque(maxlen=48)
        self._active = False
        self._started_at: datetime | None = None
        self._peak_response_ms = 0.0
        self._peak_cpu = 0.0
        self._peak_read_kb_s = 0.0
        self._peak_write_kb_s = 0.0
        self._hung_count = 0
        self._slow_count = 0
        self._event_type = "COMPAT_STALL"
        self._calm_ms = 0.0
        self._target_process = ""

    @Slot(object)
    def ingest_sample(self, sample: CompatibilitySample):
        self._recent.append(sample)
        self._target_process = sample.target_process or self._target_process
        triggered, event_type = self._is_stutter(sample)
        if triggered:
            self._calm_ms = 0.0
            self._peak_response_ms = max(self._peak_response_ms, sample.response_time_ms)
            self._peak_cpu = max(self._peak_cpu, sample.process_cpu_percent)
            self._peak_read_kb_s = max(self._peak_read_kb_s, sample.process_read_kb_s)
            self._peak_write_kb_s = max(self._peak_write_kb_s, sample.process_write_kb_s)
            self._hung_count += 1 if sample.is_hung else 0
            self._slow_count += 1 if sample.response_time_ms >= RESPONSE_SPIKE_MS else 0
            if event_type == "COMPAT_WINDOW_HANG" or self._event_type != "COMPAT_WINDOW_HANG":
                self._event_type = event_type
            if not self._active:
                self._active = True
                self._started_at = sample.timestamp
                self.status_changed.emit(
                    f"{event_type} detected for {self._target_process or 'unknown process'}"
                )
                self.stutter_started.emit(sample.timestamp)
            return

        if self._active:
            self._calm_ms += max(sample.response_time_ms, 50.0)
            if self._calm_ms >= CALM_TIME_TO_END_MS:
                self._finish_episode(sample.timestamp)

    @Slot()
    def reset_target(self):
        if self._active and self._recent:
            self._finish_episode(self._recent[-1].timestamp)
            return
        self._clear_state()

    def _clear_state(self):
        self._recent.clear()
        self._active = False
        self._started_at = None
        self._target_process = ""
        self._peak_response_ms = 0.0
        self._peak_cpu = 0.0
        self._peak_read_kb_s = 0.0
        self._peak_write_kb_s = 0.0
        self._hung_count = 0
        self._slow_count = 0
        self._event_type = "COMPAT_STALL"
        self._calm_ms = 0.0

    def _is_stutter(self, sample: CompatibilitySample) -> tuple[bool, str]:
        if sample.is_hung or sample.response_time_ms >= RESPONSE_FREEZE_MS:
            return True, "COMPAT_WINDOW_HANG"
        if sample.visual_frozen_streak >= VISUAL_FROZEN_STREAK_LIMIT and sample.response_time_ms >= 40.0:
            return True, "COMPAT_VISUAL_FREEZE"
        if sample.response_time_ms >= RESPONSE_SPIKE_MS:
            return True, "COMPAT_STALL"
        if (
            per_core_to_machine_share(sample.process_cpu_percent) >= CPU_PRESSURE_MACHINE_SHARE
            and sample.response_time_ms >= 60.0
        ):
            return True, "COMPAT_CPU_PRESSURE"
        if (sample.process_read_kb_s + sample.process_write_kb_s) >= 4096.0 and sample.response_time_ms >= 60.0:
            return True, "COMPAT_IO_PRESSURE"
        return False, "COMPAT_STALL"

    def _finish_episode(self, ended_at: datetime):
        episode = self._build_episode(ended_at)
        if episode is not None:
            self.stutter_ended.emit(episode)
            self.status_changed.emit(
                f"{episode.event_type} ended, peak response {episode.peak_frame_time_ms:.1f} ms"
            )
        self._clear_state()

    def _build_episode(self, ended_at: datetime) -> FrameStutterEpisode | None:
        if not self._started_at or not self._recent:
            return None
        relevant = [sample for sample in self._recent if sample.timestamp >= self._started_at]
        if not relevant:
            relevant = list(self._recent)
        response_times = [sample.response_time_ms for sample in relevant]
        response_sorted = sorted(response_times)
        p95_idx = min(len(response_sorted) - 1, max(0, int(len(response_sorted) * 0.95) - 1))
        avg_response = sum(response_times) / len(response_times)
        p95_response = response_sorted[p95_idx]
        severity = min(1.0, max(self._peak_response_ms / 400.0, p95_response / 250.0))
        explanation = self._build_explanation(avg_response, p95_response)
        category = self._classify_category()
        return FrameStutterEpisode(
            started_at=self._started_at,
            ended_at=ended_at,
            target_process=self._target_process,
            event_type=self._event_type,
            peak_frame_time_ms=self._peak_response_ms,
            avg_frame_time_ms=avg_response,
            p95_frame_time_ms=p95_response,
            slow_frame_count=self._slow_count,
            freeze_frame_count=self._hung_count,
            peak_cpu_wait_ms=0.0,
            peak_gpu_busy_ms=0.0,
            present_mode="compatibility",
            severity=severity,
            explanation=explanation,
            category=category,
            scope="LOCAL",
        )

    def _build_explanation(self, avg_response: float, p95_response: float) -> str:
        if self._event_type == "COMPAT_WINDOW_HANG":
            hint = "The game window stopped responding to the message pump."
        elif self._event_type == "COMPAT_IO_PRESSURE":
            hint = "Process I/O pressure lined up with a visible responsiveness stall."
        elif self._event_type == "COMPAT_CPU_PRESSURE":
            hint = "Process CPU pressure lined up with a visible responsiveness stall."
        else:
            hint = "The foreground game became sluggish even without PresentMon frame data."
        return (
            f"{hint} Peak response delay reached {self._peak_response_ms:.1f} ms, "
            f"average response delay was {avg_response:.1f} ms, P95 was {p95_response:.1f} ms. "
            f"Peak CPU usage was {self._peak_cpu:.1f}%, peak read throughput was {self._peak_read_kb_s:.0f} KB/s, "
            f"peak write throughput was {self._peak_write_kb_s:.0f} KB/s."
        )

    def _classify_category(self) -> str:
        if self._event_type == "COMPAT_CPU_PRESSURE":
            return "CPU_BOUND"
        if self._event_type == "COMPAT_IO_PRESSURE":
            return "IO_STALL"
        if self._event_type in {"COMPAT_WINDOW_HANG", "COMPAT_VISUAL_FREEZE"}:
            return "LOCAL_STUTTER"
        return "LOCAL_STUTTER"
