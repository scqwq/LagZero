"""
core/frame_detector.py — Frame-time based stutter detector.

Consumes FrameSample items from PresentMon and raises player-visible frame
events such as spikes, stutters, and freezes. This detector is intentionally
game-centric: frame pacing is the primary signal, while CPU/GPU wait hints are
captured to support later root-cause analysis.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QObject, Signal, Slot

from core.models import FrameSample, FrameStutterEpisode


SPIKE_FRAME_MS = 50.0
STUTTER_FRAME_MS = 66.0
FREEZE_FRAME_MS = 150.0
P95_BAD_FRAME_MS = 33.0
MIN_WINDOW_SAMPLES = 12
CALM_TIME_TO_END_MS = 700.0


class FrameStutterDetector(QObject):
    stutter_started = Signal(object)
    stutter_ended = Signal(object)
    status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recent_frames: deque[FrameSample] = deque(maxlen=180)
        self._active = False
        self._started_at: datetime | None = None
        self._target_process = ""
        self._peak_frame_time = 0.0
        self._peak_cpu_wait = 0.0
        self._peak_gpu_busy = 0.0
        self._slow_frame_count = 0
        self._freeze_frame_count = 0
        self._peak_kind = "FRAME_SPIKE"
        self._calm_ms = 0.0
        self._present_mode = ""

    @Slot(object)
    def ingest_frame(self, sample: FrameSample):
        self._recent_frames.append(sample)
        self._target_process = sample.process_name or self._target_process
        self._present_mode = sample.present_mode or self._present_mode

        triggered, event_kind = self._is_stutter(sample)
        if triggered:
            self._calm_ms = 0.0
            self._peak_frame_time = max(self._peak_frame_time, sample.frame_time_ms)
            self._peak_cpu_wait = max(self._peak_cpu_wait, sample.cpu_wait_ms)
            self._peak_gpu_busy = max(self._peak_gpu_busy, sample.gpu_busy_ms)
            self._slow_frame_count += 1 if sample.frame_time_ms >= SPIKE_FRAME_MS else 0
            self._freeze_frame_count += 1 if sample.frame_time_ms >= FREEZE_FRAME_MS else 0
            if self._event_priority(event_kind) > self._event_priority(self._peak_kind):
                self._peak_kind = event_kind

            if not self._active:
                self._active = True
                self._started_at = sample.timestamp
                self.status_changed.emit(
                    f"{event_kind} detected for {self._target_process or 'unknown process'}"
                )
                self.stutter_started.emit(sample.timestamp)
        elif self._active:
            self._calm_ms += max(sample.frame_time_ms, 1.0)
            if self._calm_ms >= CALM_TIME_TO_END_MS:
                self._finish_episode(sample.timestamp)

    @Slot()
    def reset_target(self):
        if self._active and self._recent_frames:
            self._finish_episode(self._recent_frames[-1].timestamp)
        self._recent_frames.clear()
        self._target_process = ""
        self._present_mode = ""

    def _finish_episode(self, ended_at: datetime):
        episode = self._build_episode(ended_at)
        if episode is not None:
            self.stutter_ended.emit(episode)
            self.status_changed.emit(
                f"{episode.event_type} ended, peak {episode.peak_frame_time_ms:.1f} ms"
            )
        self._active = False
        self._started_at = None
        self._peak_frame_time = 0.0
        self._peak_cpu_wait = 0.0
        self._peak_gpu_busy = 0.0
        self._slow_frame_count = 0
        self._freeze_frame_count = 0
        self._peak_kind = "FRAME_SPIKE"
        self._calm_ms = 0.0

    def _build_episode(self, ended_at: datetime) -> FrameStutterEpisode | None:
        if not self._started_at or not self._recent_frames:
            return None
        relevant = [f for f in self._recent_frames if f.timestamp >= self._started_at]
        if not relevant:
            relevant = list(self._recent_frames)
        frame_times = [f.frame_time_ms for f in relevant]
        frame_times_sorted = sorted(frame_times)
        p95_idx = min(len(frame_times_sorted) - 1, max(0, int(len(frame_times_sorted) * 0.95) - 1))
        avg_frame = sum(frame_times) / len(frame_times)
        p95_frame = frame_times_sorted[p95_idx]
        severity = min(1.0, max(self._peak_frame_time / 200.0, p95_frame / 80.0))
        explanation = self._build_explanation(avg_frame, p95_frame)
        return FrameStutterEpisode(
            started_at=self._started_at,
            ended_at=ended_at,
            target_process=self._target_process,
            event_type=self._peak_kind,
            peak_frame_time_ms=self._peak_frame_time,
            avg_frame_time_ms=avg_frame,
            p95_frame_time_ms=p95_frame,
            slow_frame_count=self._slow_frame_count,
            freeze_frame_count=self._freeze_frame_count,
            peak_cpu_wait_ms=self._peak_cpu_wait,
            peak_gpu_busy_ms=self._peak_gpu_busy,
            present_mode=self._present_mode,
            severity=severity,
            explanation=explanation,
        )

    def _build_explanation(self, avg_frame: float, p95_frame: float) -> str:
        hint = "Frame pacing was unstable."
        if self._peak_frame_time >= FREEZE_FRAME_MS:
            hint = "A visible freeze occurred."
        if self._peak_cpu_wait >= 20.0 and self._peak_cpu_wait > self._peak_gpu_busy:
            cause_hint = "The frame queue spent more time waiting on the CPU/render thread side."
        elif self._peak_gpu_busy >= 12.0 and self._peak_gpu_busy >= self._peak_cpu_wait:
            cause_hint = "GPU-side rendering pressure was stronger than CPU wait."
        else:
            cause_hint = "No dominant CPU-wait or GPU-busy spike stood out yet."
        return (
            f"{hint} Peak frame time reached {self._peak_frame_time:.1f} ms, "
            f"average frame time was {avg_frame:.1f} ms, P95 was {p95_frame:.1f} ms, "
            f"slow frames counted: {self._slow_frame_count}, freeze frames counted: {self._freeze_frame_count}. "
            f"Peak CPU wait was {self._peak_cpu_wait:.1f} ms, peak GPU busy was {self._peak_gpu_busy:.1f} ms, "
            f"present mode: {self._present_mode or 'unknown'}. {cause_hint}"
        )

    def _is_stutter(self, sample: FrameSample) -> tuple[bool, str]:
        if sample.frame_time_ms >= FREEZE_FRAME_MS:
            return True, "FRAME_FREEZE"
        if sample.frame_time_ms >= STUTTER_FRAME_MS:
            return True, "FRAME_STUTTER"

        if len(self._recent_frames) < MIN_WINDOW_SAMPLES:
            return sample.frame_time_ms >= SPIKE_FRAME_MS, "FRAME_SPIKE"

        recent = list(self._recent_frames)[-MIN_WINDOW_SAMPLES:]
        frame_times = [f.frame_time_ms for f in recent]
        slow_count = sum(1 for ft in frame_times if ft >= SPIKE_FRAME_MS)
        p95 = sorted(frame_times)[max(0, int(len(frame_times) * 0.95) - 1)]

        if slow_count >= 3 or p95 >= P95_BAD_FRAME_MS:
            event_kind = "FRAME_STUTTER" if p95 >= STUTTER_FRAME_MS or slow_count >= 5 else "FRAME_SPIKE"
            return True, event_kind
        return False, "FRAME_SPIKE"

    @staticmethod
    def _event_priority(kind: str) -> int:
        return {
            "FRAME_SPIKE": 1,
            "FRAME_STUTTER": 2,
            "FRAME_FREEZE": 3,
        }.get(kind, 0)
