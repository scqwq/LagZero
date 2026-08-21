"""
core/frame_attribution.py — Which stage of the frame pipeline caused a stutter.

The detector knows *when* a frame went long. This module answers *where* the
extra milliseconds went, by comparing each pipeline stage at the worst frame
against what that stage normally costs in this same session.

Working in excess-over-baseline rather than absolute milliseconds is the whole
point. A 12 ms GPUBusy is unremarkable in a 60 fps game and catastrophic in a
240 Hz one, so any fixed threshold is wrong for somebody. What matters is which
stage *grew* when the frame grew.

Stage buckets, and why CPU wait is not its own verdict:
  CPUBusy excess   -> the game's own CPU work got heavier          (CPU_BOUND)
  GPUBusy excess   -> rendering got heavier                        (GPU_BOUND)
  CPUWait excess   -> the CPU was blocked. Blocked *by the GPU* is a GPU
                      problem (queue backpressure), so the part of the wait the
                      GPU growth can explain is folded into the GPU bucket. Only
                      the unexplained remainder becomes a present-path stall.
  Display excess   -> the frame was submitted on time but the screen did not
                      update (dropped frames, composition hiccups).
"""
from __future__ import annotations

from dataclasses import dataclass

from core.models import FrameAttribution

CATEGORY_CPU = "CPU_BOUND"
CATEGORY_GPU = "GPU_BOUND"
CATEGORY_PRESENT_BLOCKED = "DRIVER_RENDER_PATH"
CATEGORY_DISPLAY = "DISPLAY_PIPELINE"
CATEGORY_UNDETERMINED = "UNDETERMINED"

# Below this there is nothing to explain: the stages simply did not grow.
MIN_TOTAL_EXCESS_MS = 1.5
# Excess a player has a chance of noticing. Used only to temper confidence, not
# to gate detection — the detector already decided the frame was bad.
PERCEPTIBLE_EXCESS_MS = 8.0
# A stage must hold at least this share of the excess to be worth a line in the
# report. Keeps the evidence list at two or three lines instead of always four.
EVIDENCE_SHARE_FLOOR = 0.12


@dataclass
class StageReading:
    """One stage's value at the worst frame, next to what it normally costs."""

    peak: float = 0.0
    baseline: float = 0.0

    @property
    def excess(self) -> float:
        return max(0.0, self.peak - self.baseline)


def attribute_stutter(
    *,
    frame: StageReading,
    cpu_busy: StageReading,
    cpu_wait: StageReading,
    gpu_busy: StageReading,
    display: StageReading,
    dropped_ratio: float = 0.0,
    baseline_ready: bool = True,
    has_frame_breakdown: bool = True,
) -> FrameAttribution:
    """Split the worst frame's excess time across pipeline stages."""
    if not has_frame_breakdown:
        return FrameAttribution(
            category=CATEGORY_UNDETERMINED,
            confidence=0.0,
            evidence=["No per-frame CPU/GPU breakdown was available for this capture."],
        )

    cpu_bucket = cpu_busy.excess
    gpu_bucket = gpu_busy.excess
    wait_excess = cpu_wait.excess
    # Wait the GPU can account for is backpressure, not an independent stall.
    explained_by_gpu = min(wait_excess, gpu_bucket)
    gpu_bucket += explained_by_gpu
    blocked_bucket = wait_excess - explained_by_gpu
    display_bucket = display.excess
    if dropped_ratio > 0.0:
        # A frame that never reached the screen has no display interval to grow,
        # so the drop itself has to carry the weight instead.
        display_bucket += dropped_ratio * max(frame.baseline, 1.0)

    buckets = {
        CATEGORY_CPU: cpu_bucket,
        CATEGORY_GPU: gpu_bucket,
        CATEGORY_PRESENT_BLOCKED: blocked_bucket,
        CATEGORY_DISPLAY: display_bucket,
    }
    total = sum(buckets.values())
    if total < MIN_TOTAL_EXCESS_MS:
        return FrameAttribution(
            category=CATEGORY_UNDETERMINED,
            confidence=0.0,
            evidence=[_unexplained_line(frame)],
        )

    ranked = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
    category, dominant = ranked[0]
    runner_up = ranked[1][1]
    share = dominant / total
    margin = (dominant - runner_up) / total
    confidence = 0.30 + 0.45 * share + 0.25 * margin
    if not baseline_ready:
        # Without enough calm frames the "normal" being compared against is a
        # guess, so the verdict should not be stated with full authority.
        confidence *= 0.75
    if total < PERCEPTIBLE_EXCESS_MS:
        confidence *= 0.80
    confidence = round(min(0.95, max(0.05, confidence)), 2)

    return FrameAttribution(
        category=category,
        confidence=confidence,
        evidence=_evidence_lines(
            frame, buckets, total, dropped_ratio, baseline_ready
        ),
        cpu_share=round(cpu_bucket / total, 3),
        gpu_share=round(gpu_bucket / total, 3),
        display_share=round(display_bucket / total, 3),
        wait_share=round(blocked_bucket / total, 3),
    )


def _unexplained_line(frame: StageReading) -> str:
    if frame.excess <= 0.0:
        return "No stage grew measurably during this episode."
    return (
        f"Frame time rose {frame.excess:.1f} ms above its {frame.baseline:.1f} ms norm, "
        f"but no single pipeline stage grew with it."
    )


def _evidence_lines(
    frame: StageReading,
    buckets: dict[str, float],
    total: float,
    dropped_ratio: float,
    baseline_ready: bool = True,
) -> list[str]:
    lines: list[str] = []
    # On a pure drop episode (healthy frame time, frames never shown) the
    # frame-vs-norm comparison is meaningless — 4.1 ms against a 4.1 ms norm says
    # nothing. Lead with what actually happened instead.
    if frame.excess >= MIN_TOTAL_EXCESS_MS:
        lines.append(
            f"Frame time peaked at {frame.peak:.1f} ms against a {frame.baseline:.1f} ms norm."
        )
    elif dropped_ratio > 0.0:
        lines.append(
            f"Frame time stayed near its {frame.baseline:.1f} ms norm, "
            f"but the screen did not show every frame."
        )
    else:
        lines.append(_unexplained_line(frame))
    templates = {
        CATEGORY_CPU: "the game's own CPU work",
        CATEGORY_GPU: "GPU rendering (including the wait it forced on the CPU)",
        CATEGORY_PRESENT_BLOCKED: "time blocked in the present path, not explained by GPU load",
        CATEGORY_DISPLAY: "frames reaching the screen late or not at all",
    }
    for name, value in sorted(buckets.items(), key=lambda item: item[1], reverse=True):
        if total <= 0.0 or value / total < EVIDENCE_SHARE_FLOOR:
            continue
        lines.append(
            f"{value / total * 100:.0f}% of the extra time went to {templates[name]} "
            f"({value:.1f} ms)."
        )
    if dropped_ratio > 0.0:
        lines.append(f"{dropped_ratio * 100:.0f}% of frames never reached the screen.")
    if not baseline_ready:
        lines.append(
            "Confidence is limited: not enough calm frames were seen to learn this game's normal rhythm."
        )
    return lines
