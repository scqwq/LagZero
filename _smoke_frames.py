"""Throwaway smoke check for the baseline-adaptive frame detector."""
import sys
from datetime import datetime, timedelta

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv)

from core.frame_detector import FrameStutterDetector
from core.models import FrameSample

BASE = datetime.now()


def frame(i, ft, cpu_busy=1.0, cpu_wait=0.5, gpu_busy=1.0, gpu_wait=0.0,
          displayed=None, was_displayed=True):
    return FrameSample(
        timestamp=BASE + timedelta(milliseconds=i * 4),
        process_name="game.exe",
        process_id=1,
        swap_chain="0x1",
        runtime="DXGI",
        present_mode="Hardware: Independent Flip",
        sync_interval=0,
        allows_tearing=True,
        frame_time_ms=ft,
        cpu_busy_ms=cpu_busy,
        cpu_wait_ms=cpu_wait,
        gpu_busy_ms=gpu_busy,
        gpu_wait_ms=gpu_wait,
        displayed_time_ms=ft if displayed is None else displayed,
        was_displayed=was_displayed,
    )


def run(label, frames):
    det = FrameStutterDetector()
    episodes = []
    det.stutter_ended.connect(episodes.append)
    for i, f in enumerate(frames):
        det.ingest_frame(f)
    # drain: feed calm frames so the episode closes
    for j in range(400):
        det.ingest_frame(frame(len(frames) + j, 4.1))
    print(f"\n=== {label} ===")
    if not episodes:
        print("  no episode")
        return
    ep = episodes[0]
    print(f"  type={ep.event_type} category={ep.category!r} sev={ep.severity}")
    print(f"  peak={ep.peak_frame_time_ms:.1f} baseline={ep.baseline_frame_time_ms:.2f} "
          f"threshold={ep.stutter_threshold_ms:.1f} dropped={ep.dropped_frame_count}")
    a = ep.attribution
    print(f"  attribution={a.category} conf={a.confidence} cpu={a.cpu_share} "
          f"gpu={a.gpu_share} wait={a.wait_share} disp={a.display_share}")
    for line in a.evidence:
        print(f"    - {line}")


# 240 Hz baseline: 4.1 ms normal, then a 30 ms GPU-driven hitch.
calm240 = [frame(i, 4.1, gpu_busy=1.2) for i in range(200)]
gpu_hitch = [frame(200 + i, 30.0, cpu_busy=1.2, cpu_wait=2.0, gpu_busy=28.0) for i in range(4)]
run("240Hz GPU hitch (was invisible with fixed 66ms thresholds)", calm240 + gpu_hitch)

# Same baseline, CPU-driven hitch.
cpu_hitch = [frame(200 + i, 30.0, cpu_busy=29.0, cpu_wait=0.4, gpu_busy=1.3) for i in range(4)]
run("240Hz CPU hitch", calm240 + cpu_hitch)

# Present-path block: CPU waits a long time, GPU is idle.
blocked = [frame(200 + i, 30.0, cpu_busy=1.1, cpu_wait=27.0, gpu_busy=1.2) for i in range(4)]
run("240Hz present-path block", calm240 + blocked)

# Frames submitted on time but never displayed.
dropped = [frame(200 + i, 4.1, was_displayed=False, displayed=0.0) for i in range(10)]
run("dropped frames at healthy frame time", calm240 + dropped)

# 30 fps game: ordinary jitter must NOT fire.
calm30 = [frame(i, 33.3 + (i % 3) * 0.6, gpu_busy=20.0) for i in range(200)]
run("30fps ordinary jitter (must be quiet)", calm30)

# 30 fps game with a real freeze.
freeze30 = [frame(200 + i, 260.0, cpu_busy=30.0, gpu_busy=200.0) for i in range(2)]
run("30fps real freeze", calm30 + freeze30)
