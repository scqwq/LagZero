"""
core/frame_detector.py — Frame-time based stutter detector.

Consumes FrameSample items from PresentMon and raises player-visible frame
events such as spikes, stutters, and freezes.

Thresholds are learned, not fixed. The previous version compared every frame
against 50/66/150 ms, which meant a 240 Hz player whose frame time jumped from
4 ms to 30 ms — a very visible hitch — was never flagged, while a 30 fps game
sat permanently near the spike line. Each stage now carries a WelfordBaseline of
what it normally costs in this session, and a frame is judged against that norm
plus a perceptibility floor, so the detector adapts to the game's own frame rate
without firing on ordinary jitter.

Two signals are tracked separately:
  frame time    — how long the game took to produce and submit the frame.
  display gap   — how long the screen actually went without updating. A frame can
                  be submitted on time and still never reach the screen, which is
                  a stutter the player sees and frame time alone cannot show.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QObject, Signal, Slot

from core.baseline import WelfordBaseline
from core.frame_attribution import StageReading, attribute_stutter
from core.models import FrameSample, FrameStutterEpisode

# Floors keep the detector honest about human perception. On a perfectly steady
# capture the standard deviation approaches zero, and a pure mean+Nσ rule would
# then call sub-millisecond jitter a stutter.
SPIKE_FLOOR_MS = 20.0
STUTTER_FLOOR_MS = 33.0
FREEZE_FLOOR_MS = 150.0
# Warmup thresholds: absolute values used until the baseline has enough samples
# to know this game's rhythm. A 30 fps game's normal 33.3 ms frame sits exactly
# on STUTTER_FLOOR_MS, so using the floors during warmup flagged every single
# frame of a slow game AND kept them out of the baseline (stutter frames are not
# learned), which meant the baseline never became ready. These mirror the old
# fixed detector: conservative, wrong for nobody who wasn't already covered.
WARMUP_SPIKE_MS = 50.0
WARMUP_STUTTER_MS = 66.0

# Additive margins are what make the thresholds work at both ends of the refresh
# range: 4 ms + 14 ms flags a 5x hitch at 240 Hz, 33 ms + 14 ms does not flag
# ordinary variance at 30 fps.
SPIKE_MARGIN_MS = 14.0
STUTTER_MARGIN_MS = 28.0
FREEZE_MARGIN_MS = 120.0
# Ratio lines, same idea as the margins but multiplicative: a frame costing
# several times its own norm is a hitch at ANY refresh rate.
SPIKE_RATIO = 2.0
STUTTER_RATIO = 3.5
DISPLAY_MARGIN_MS = 8.0
# Display floors are a RATIO, not milliseconds: a fixed 22 ms floor would flag
# every frame of a 30 fps game (33 ms display intervals are normal there). What
# is perceptible is the screen going quiet for much longer than its own rhythm.
DISPLAY_FLOOR_RATIO = 2.2
DISPLAY_RECENT_WINDOW_SAMPLES = 10
DISPLAY_MINOR_CLUSTER_COUNT = 2
DISPLAY_MAJOR_CLUSTER_COUNT = 3

SPIKE_SIGMAS = 3.0
STUTTER_SIGMAS = 5.0
DISPLAY_SIGMAS = 4.5
# A frame submitted quickly but shown much later than it was submitted. The
# display gap only means something independent when it exceeds the frame's own
# cost — otherwise it is just the frame being slow, which the frame thresholds
# already catch, and the two detectors would fire together on one event.
DISPLAY_LAG_RATIO = 1.8
DISPLAY_LAG_FLOOR_MS = 12.0
DISPLAY_MAJOR_EXCESS_MS = 28.0
DISPLAY_MAJOR_RATIO = 2.4
DISPLAY_DIRECT_EXCESS_MS = 42.0
DISPLAY_DIRECT_RATIO = 3.0
DISPLAY_FREEZE_EXCESS_MS = 120.0
DISPLAY_FREEZE_GAP_MS = 180.0

BASELINE_MIN_FRAMES = 120
MIN_WINDOW_SAMPLES = 12
CALM_TIME_TO_END_MS = 700.0
CLUSTER_SLOW_COUNT = 3
CLUSTER_STUTTER_COUNT = 5
# A single dropped frame is a 4 ms longer gap at 240 Hz — invisible. Its
# visible consequence (the previous frame staying up twice as long) is already
# caught by the display-gap thresholds below, so the drop itself only counts as
# an event when it comes as a burst. Was_displayed is False for every dropped
# frame in the window, drops included.
DROP_CLUSTER_COUNT = 3


class FrameStutterDetector(QObject):
    stutter_started = Signal(object)
    stutter_ended = Signal(object)
    status_changed = Signal(str)

    def __init__(self, parent=None, spike_ratio: float = SPIKE_RATIO, stutter_ratio: float = STUTTER_RATIO):
        super().__init__(parent)
        self._spike_ratio = max(1.2, float(spike_ratio))
        self._stutter_ratio = max(1.5, float(stutter_ratio))
        self._recent_frames: deque[FrameSample] = deque(maxlen=180)
        self._active = False
        self._started_at: datetime | None = None
        self._target_process = ""
        self._present_mode = ""
        # One baseline per stage. Attribution compares a stage's peak against its
        # own norm, so each needs its own accumulator rather than a shared one.
        self._frame_base = WelfordBaseline(min_samples=BASELINE_MIN_FRAMES)
        self._cpu_busy_base = WelfordBaseline(min_samples=BASELINE_MIN_FRAMES)
        self._cpu_wait_base = WelfordBaseline(min_samples=BASELINE_MIN_FRAMES)
        self._gpu_busy_base = WelfordBaseline(min_samples=BASELINE_MIN_FRAMES)
        self._display_base = WelfordBaseline(min_samples=BASELINE_MIN_FRAMES)
        self._reset_episode_state()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    @Slot(object)
    def ingest_frame(self, sample: FrameSample):
        self._recent_frames.append(sample)
        self._target_process = sample.process_name or self._target_process
        self._present_mode = sample.present_mode or self._present_mode
        if sample.metrics_version:
            self._metrics_version = sample.metrics_version

        triggered, event_kind = self._is_stutter(sample)
        # Only calm frames train the baseline. Learning from the stutter itself
        # would raise the bar every time the game misbehaved, and the detector
        # would gradually stop reporting a game that stutters constantly.
        if not triggered:
            self._learn(sample)

        if triggered:
            self._absorb(sample, event_kind)
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
        # A different game (or a different scene after a long gap) has nothing to
        # do with the old norm, so the learned baselines go with it.
        for base in self._baselines():
            base.reset()

    def _baselines(self) -> tuple[WelfordBaseline, ...]:
        return (
            self._frame_base,
            self._cpu_busy_base,
            self._cpu_wait_base,
            self._gpu_busy_base,
            self._display_base,
        )

    @Slot(float, float)
    def update_sensitivity(self, spike_ratio: float, stutter_ratio: float):
        """Update detection multipliers without resetting learned baselines."""
        self._spike_ratio = max(1.2, float(spike_ratio))
        self._stutter_ratio = max(1.5, float(stutter_ratio))

    def _learn(self, sample: FrameSample):
        self._frame_base.add(sample.frame_time_ms)
        self._cpu_busy_base.add(sample.cpu_busy_ms)
        self._cpu_wait_base.add(sample.cpu_wait_ms)
        self._gpu_busy_base.add(sample.gpu_busy_ms)
        if sample.was_displayed:
            self._display_base.add(sample.displayed_time_ms)

    def _absorb(self, sample: FrameSample, event_kind: str):
        """Fold one bad frame into the running peaks for the current episode."""
        self._calm_ms = 0.0
        self._peak_frame_time = max(self._peak_frame_time, sample.frame_time_ms)
        self._peak_cpu_busy = max(self._peak_cpu_busy, sample.cpu_busy_ms)
        self._peak_cpu_wait = max(self._peak_cpu_wait, sample.cpu_wait_ms)
        self._peak_gpu_busy = max(self._peak_gpu_busy, sample.gpu_busy_ms)
        self._peak_gpu_wait = max(self._peak_gpu_wait, sample.gpu_wait_ms)
        self._peak_input_latency = max(self._peak_input_latency, sample.input_latency_ms)
        self._episode_frames += 1
        if sample.was_displayed:
            self._peak_display_gap = max(self._peak_display_gap, sample.displayed_time_ms)
            self._peak_display_excess = max(
                self._peak_display_excess,
                self._display_excess(sample),
            )
            if event_kind == "DISPLAY_STALL":
                if self._last_display_stall_level >= 2:
                    self._display_stall_major_count += 1
                elif self._last_display_stall_level >= 1:
                    self._display_stall_minor_count += 1
        else:
            self._dropped_frames += 1
        if sample.frame_time_ms >= self._spike_threshold():
            self._slow_frame_count += 1
        if sample.frame_time_ms >= self._freeze_threshold():
            self._freeze_frame_count += 1
        if self._event_priority(event_kind) > self._event_priority(self._peak_kind):
            self._peak_kind = event_kind

    # ------------------------------------------------------------------
    # Thresholds
    # ------------------------------------------------------------------

    def _spike_threshold(self) -> float:
        if not self._frame_base.is_ready:
            return WARMUP_SPIKE_MS
        return self._threshold(
            self._frame_base, SPIKE_SIGMAS, SPIKE_MARGIN_MS, self._spike_ratio, SPIKE_FLOOR_MS
        )

    def _stutter_threshold(self) -> float:
        if not self._frame_base.is_ready:
            return WARMUP_STUTTER_MS
        return self._threshold(
            self._frame_base, STUTTER_SIGMAS, STUTTER_MARGIN_MS, self._stutter_ratio, STUTTER_FLOOR_MS
        )

    def _freeze_threshold(self) -> float:
        # A freeze is absolute: 150 ms of nothing is a freeze in any game. The
        # baseline only ever raises this, never lowers it.
        if not self._frame_base.is_ready:
            return FREEZE_FLOOR_MS
        return max(FREEZE_FLOOR_MS, self._frame_base.mean + FREEZE_MARGIN_MS)

    def _display_threshold(self) -> float:
        """
        Display-gap line, in the same units as the frame-time line.

        Scaled by the learned display norm instead of a fixed millisecond floor:
        33 ms gaps are a 30 fps game's healthy rhythm and must not raise the
        flag, while the same 33 ms at 240 Hz is the screen going quiet for eight
        frames. The fixed addend covers a steady capture where std≈0.
        """
        base = self._display_base
        if not base.is_ready or base.mean <= 0.0:
            # No rhythm learned yet; a 2x multiple of nothing is nothing, so
            # until ready only genuine dropped frames (was_displayed == False)
            # count on the display side.
            return float("inf")
        return max(
            base.mean * DISPLAY_FLOOR_RATIO,
            base.mean + DISPLAY_MARGIN_MS,
            base.mean + DISPLAY_SIGMAS * base.std,
        )

    @staticmethod
    def _threshold(
        base: WelfordBaseline, sigmas: float, margin: float, ratio: float, floor: float
    ) -> float:
        """
        max(floor, mean*ratio, mean + margin, mean + sigmas*std).

        The ratio term scales with the game's own rhythm (a 3.5x norm at 4 ms and
        at 33 ms both mean "a visible hitch"). The margin term covers a steady
        capture where std≈0 and 2x would flag sub-millisecond jitter. The sigma
        term covers a naturally jittery game, where a fixed margin would fire
        constantly. The floor is only a backstop, never the deciding term.
        """
        if not base.is_ready or base.mean <= 0.0:
            return floor
        return max(
            floor,
            base.mean * ratio,
            base.mean + margin,
            base.mean + sigmas * base.std,
        )

    def _is_stutter(self, sample: FrameSample) -> tuple[bool, str]:
        frame_ms = sample.frame_time_ms
        if frame_ms >= self._freeze_threshold():
            return True, "FRAME_FREEZE"
        if frame_ms >= self._stutter_threshold():
            return True, "FRAME_STUTTER"

        # Submitted fine but never shown: previously invisible to this detector,
        # because frame_time_ms looks perfectly healthy on a dropped frame.
        # One dropped frame is a normal part of present-queue life; a run of
        # them inside a few frames is the visible "image tearing into a skip".
        if not sample.was_displayed:
            # The window already contains this frame (ingest_frame appends
            # before judging), so recent_drops counts it; no +1. Deques cannot
            # be sliced, hence the list() the cluster checks also use.
            if len(self._recent_frames) < MIN_WINDOW_SAMPLES:
                return False, "FRAME_SPIKE"
            recent_drops = sum(
                1 for f in list(self._recent_frames)[-MIN_WINDOW_SAMPLES:] if not f.was_displayed
            )
            if recent_drops >= DROP_CLUSTER_COUNT:
                return True, "FRAME_DROP"
            return False, "FRAME_SPIKE"
        # A long display gap that merely tracks a slow frame is not an
        # independent finding — the frame thresholds above already reported it.
        # The display side is only its own stutter class when the screen went
        # quiet for longer than the frame itself cost (submitted on time, shown
        # late) or beyond this game's learned display rhythm.
        display_level = self._display_stall_level(sample)
        if display_level > 0:
            self._last_display_stall_level = display_level
            return True, "DISPLAY_STALL"

        spike_threshold = self._spike_threshold()
        if len(self._recent_frames) < MIN_WINDOW_SAMPLES:
            return frame_ms >= spike_threshold, "FRAME_SPIKE"

        # A cluster of individually-tolerable slow frames is what a player reads
        # as "the game is choppy", so it counts even when no single frame crosses
        # the stutter line.
        recent = list(self._recent_frames)[-MIN_WINDOW_SAMPLES:]
        slow_count = sum(1 for f in recent if f.frame_time_ms >= spike_threshold)
        if frame_ms >= spike_threshold or slow_count >= CLUSTER_SLOW_COUNT:
            if slow_count >= CLUSTER_STUTTER_COUNT:
                return True, "FRAME_STUTTER"
            if frame_ms >= spike_threshold or slow_count >= CLUSTER_SLOW_COUNT:
                return True, "FRAME_SPIKE"
        return False, "FRAME_SPIKE"

    # ------------------------------------------------------------------
    # Episode assembly
    # ------------------------------------------------------------------

    def _reset_episode_state(self):
        self._peak_frame_time = 0.0
        self._peak_cpu_busy = 0.0
        self._peak_cpu_wait = 0.0
        self._peak_gpu_busy = 0.0
        self._peak_gpu_wait = 0.0
        self._peak_display_gap = 0.0
        self._peak_display_excess = 0.0
        self._peak_input_latency = 0.0
        self._slow_frame_count = 0
        self._freeze_frame_count = 0
        self._dropped_frames = 0
        self._episode_frames = 0
        self._display_stall_minor_count = 0
        self._display_stall_major_count = 0
        self._peak_kind = "FRAME_SPIKE"
        self._calm_ms = 0.0
        self._last_display_stall_level = 0
        self._metrics_version = getattr(self, "_metrics_version", "v2")

    def _finish_episode(self, ended_at: datetime):
        episode = self._build_episode(ended_at)
        if episode is not None:
            self.stutter_ended.emit(episode)
            self.status_changed.emit(
                f"{episode.event_type} ended, peak {episode.peak_frame_time_ms:.1f} ms"
            )
        self._active = False
        self._started_at = None
        self._reset_episode_state()

    def _build_episode(self, ended_at: datetime) -> FrameStutterEpisode | None:
        if not self._started_at or not self._recent_frames:
            return None
        relevant = [f for f in self._recent_frames if f.timestamp >= self._started_at]
        if not relevant:
            relevant = list(self._recent_frames)
        frame_times = sorted(f.frame_time_ms for f in relevant)
        p95_idx = min(len(frame_times) - 1, max(0, int(len(frame_times) * 0.95) - 1))
        avg_frame = sum(frame_times) / len(frame_times)
        p95_frame = frame_times[p95_idx]

        attribution = self._attribute()
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
            severity=self._severity(p95_frame),
            explanation=self._build_explanation(avg_frame, p95_frame, attribution),
            category=attribution.category if attribution.is_confident else "",
            scope="LOCAL" if attribution.is_confident else "",
            baseline_frame_time_ms=self._frame_base.mean,
            stutter_threshold_ms=self._stutter_threshold(),
            dropped_frame_count=self._dropped_frames,
            peak_display_gap_ms=self._peak_display_gap,
            peak_cpu_busy_ms=self._peak_cpu_busy,
            peak_gpu_wait_ms=self._peak_gpu_wait,
            peak_input_latency_ms=self._peak_input_latency,
            display_stall_minor_count=self._display_stall_minor_count,
            display_stall_major_count=self._display_stall_major_count,
            peak_display_excess_ms=self._peak_display_excess,
            attribution=attribution,
        )

    def _severity(self, p95_frame: float) -> float:
        """
        How bad this was, relative to what this game normally runs at.

        Absolute milliseconds cannot express severity across refresh rates: a
        200 ms peak scores 1.0 at 30 fps and at 240 Hz, even though the second
        player lost 50 frames and the first lost 6. Falls back to the old
        absolute scale until the baseline is ready.
        """
        base = self._frame_base.mean
        if self._frame_base.is_ready and base > 0.0:
            ratio = max(self._peak_frame_time / base, p95_frame / base)
            severity = min(1.0, (ratio - 1.0) / 9.0)
        else:
            severity = min(1.0, max(self._peak_frame_time / 200.0, p95_frame / 80.0))
        if self._dropped_frames and self._episode_frames:
            severity = max(severity, min(1.0, self._dropped_frames / self._episode_frames))
        return round(max(0.05, severity), 3)

    def _attribute(self):
        """Run the stage-split attribution over this episode's peaks."""
        dropped_ratio = (
            self._dropped_frames / self._episode_frames if self._episode_frames else 0.0
        )
        # Compatibility mode and v1 captures without a CPU/GPU split cannot be
        # attributed; saying so beats guessing from absent numbers.
        has_breakdown = self._present_mode != "compatibility" and any(
            (self._peak_cpu_busy, self._peak_cpu_wait, self._peak_gpu_busy)
        )
        # The display bucket must not double-bill a slow frame. A long render
        # stretches the display gap by the same milliseconds, and counting that
        # stretch as "display" too would split one GPU hitch into 50% GPU / 50%
        # display. Only the part of the gap that outlasts the frame's own cost
        # is an independent display-side finding.
        display_excess_only = max(
            0.0, self._peak_display_gap - self._peak_frame_time
        )
        return attribute_stutter(
            frame=StageReading(self._peak_frame_time, self._frame_base.mean),
            cpu_busy=StageReading(self._peak_cpu_busy, self._cpu_busy_base.mean),
            cpu_wait=StageReading(self._peak_cpu_wait, self._cpu_wait_base.mean),
            gpu_busy=StageReading(self._peak_gpu_busy, self._gpu_busy_base.mean),
            display=StageReading(
                self._display_base.mean + display_excess_only,
                self._display_base.mean,
            ),
            dropped_ratio=dropped_ratio,
            baseline_ready=self._frame_base.is_ready,
            has_frame_breakdown=has_breakdown,
        )

    def _build_explanation(self, avg_frame: float, p95_frame: float, attribution) -> str:
        """
        One paragraph: what the player saw, then why.

        Kept deliberately compact — the report layer adds its own formatting, and
        the attribution's evidence lines already carry the per-stage detail, so
        repeating every peak here would only pad the text.
        """
        if self._peak_frame_time >= self._freeze_threshold():
            headline = "A visible freeze occurred."
        elif self._dropped_frames:
            headline = "Frames were submitted but never reached the screen."
        else:
            headline = "Frame pacing was unstable."

        baseline_note = ""
        if self._frame_base.is_ready and self._frame_base.mean > 0.0:
            baseline_note = (
                f" Normal for this session is {self._frame_base.mean:.1f} ms, "
                f"so the stutter line sat at {self._stutter_threshold():.1f} ms."
            )

        drop_note = ""
        if self._dropped_frames:
            drop_note = f" {self._dropped_frames} frame(s) were dropped before display."

        evidence = " ".join(attribution.evidence[1:]) if len(attribution.evidence) > 1 else ""
        if not evidence:
            evidence = attribution.evidence[0] if attribution.evidence else ""

        return (
            f"{headline} Peak frame time reached {self._peak_frame_time:.1f} ms, "
            f"average frame time was {avg_frame:.1f} ms, P95 was {p95_frame:.1f} ms, "
            f"slow frames counted: {self._slow_frame_count}, "
            f"freeze frames counted: {self._freeze_frame_count}."
            f"{baseline_note}{drop_note} {evidence}"
        ).strip()

    @staticmethod
    def _event_priority(kind: str) -> int:
        return {
            "FRAME_SPIKE": 1,
            "DISPLAY_STALL": 2,
            "FRAME_STUTTER": 3,
            "FRAME_DROP": 4,
            "FRAME_FREEZE": 5,
        }.get(kind, 0)

    def _display_excess(self, sample: FrameSample) -> float:
        if not sample.was_displayed:
            return 0.0
        display_norm = self._display_base.mean if self._display_base.mean > 0.0 else sample.frame_time_ms
        expected_gap = max(sample.frame_time_ms, display_norm)
        return max(0.0, sample.displayed_time_ms - expected_gap)

    def _is_minor_display_anomaly(self, sample: FrameSample) -> bool:
        if not sample.was_displayed:
            return False
        threshold = self._display_threshold()
        if threshold == float("inf"):
            return False
        display_excess = self._display_excess(sample)
        if display_excess <= 0.0:
            return False
        return sample.displayed_time_ms >= threshold and (
            sample.displayed_time_ms >= sample.frame_time_ms * DISPLAY_LAG_RATIO
            or display_excess >= DISPLAY_LAG_FLOOR_MS
        )

    def _is_major_display_anomaly(self, sample: FrameSample) -> bool:
        if not self._is_minor_display_anomaly(sample):
            return False
        threshold = self._display_threshold()
        display_excess = self._display_excess(sample)
        return (
            sample.displayed_time_ms >= max(threshold * 1.25, sample.frame_time_ms * DISPLAY_MAJOR_RATIO)
            or display_excess >= max(DISPLAY_MAJOR_EXCESS_MS, self._display_base.mean * 0.75)
        )

    def _display_stall_level(self, sample: FrameSample) -> int:
        if not self._is_minor_display_anomaly(sample):
            return 0
        threshold = self._display_threshold()
        display_excess = self._display_excess(sample)
        direct_major = (
            sample.displayed_time_ms >= max(threshold * 1.5, sample.frame_time_ms * DISPLAY_DIRECT_RATIO)
            or display_excess >= max(DISPLAY_DIRECT_EXCESS_MS, self._display_base.mean * 1.1)
        )
        if (
            sample.displayed_time_ms >= DISPLAY_FREEZE_GAP_MS
            or display_excess >= DISPLAY_FREEZE_EXCESS_MS
        ):
            return 2
        if direct_major:
            return 2

        recent_displayed = [
            frame for frame in list(self._recent_frames)[-DISPLAY_RECENT_WINDOW_SAMPLES:]
            if frame.was_displayed
        ]
        minor_hits = sum(1 for frame in recent_displayed if self._is_minor_display_anomaly(frame))
        major_hits = sum(1 for frame in recent_displayed if self._is_major_display_anomaly(frame))
        if major_hits >= DISPLAY_MAJOR_CLUSTER_COUNT:
            return 2
        if self._is_major_display_anomaly(sample) and major_hits >= DISPLAY_MINOR_CLUSTER_COUNT:
            return 2
        if minor_hits >= DISPLAY_MINOR_CLUSTER_COUNT:
            return 1
        return 0
