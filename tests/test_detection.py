"""
tests/test_detection.py — Unit tests for the detection engine and cause analyzer.

Run with:  python -m pytest tests/ -v
       or:  python tests/test_detection.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Qt app must exist before any QObject is created
from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv)

from datetime import datetime, timedelta
from core.models import FrameSample, FrameStutterEpisode, LagEvent, LagSnapshot, SystemSample, ProcessSample, TargetProcessMetrics
from core.detection import DetectionEngine
from core import analyzer as analyzer_mod
from core import collectors as collectors_mod
from core.analyzer import CauseAnalyzer
from ui.detail_panel import DetailPanelWidget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def set_cpu_count(count: int):
    """
    Pin the logical-core count used by the machine-share normalisation.

    Process-CPU thresholds are now expressed as a share of the whole machine,
    so a test that leaves the real core count in place asserts different
    things on a laptop than on a 32-thread desktop. Fixing the count makes
    the percentages in each test mean exactly what the test says.
    """
    collectors_mod._cpu_count_cache = count


def make_sample(cpu=10.0, ram=40.0, resp=10.0, processes=None, swap=0.0, target=None):
    return SystemSample(
        timestamp=datetime.now(),
        cpu_percent=cpu,
        cpu_per_core=[cpu],
        ram_percent=ram,
        ram_used_mb=ram * 160,
        ram_total_mb=16 * 1024,
        swap_percent=swap,
        responsiveness_ms=resp,
        top_processes=processes or [],
        target_process=target,
    )


def make_process(name, cpu, mem_mb=100.0, pid=1234):
    return ProcessSample(pid=pid, name=name, cpu_percent=cpu, memory_mb=mem_mb)


def make_target(name="game.exe", cpu=200.0, pid=1234):
    return TargetProcessMetrics(pid=pid, name=name, cpu_percent=cpu, memory_mb=8192.0)


def make_episode(
    category="",
    scope="",
    present_mode="hardware",
    peak_frame_time_ms=180.0,
    explanation="Frame time spiked.",
    freeze_frame_count=0,
    slow_frame_count=0,
    peak_cpu_wait_ms=0.0,
    peak_gpu_busy_ms=0.0,
):
    now = datetime.now()
    return FrameStutterEpisode(
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        target_process="game.exe",
        event_type="FRAME_STUTTER",
        peak_frame_time_ms=peak_frame_time_ms,
        avg_frame_time_ms=40.0,
        p95_frame_time_ms=90.0,
        slow_frame_count=slow_frame_count,
        freeze_frame_count=freeze_frame_count,
        peak_cpu_wait_ms=peak_cpu_wait_ms,
        peak_gpu_busy_ms=peak_gpu_busy_ms,
        present_mode=present_mode,
        severity=0.8,
        explanation=explanation,
        category=category,
        scope=scope,
    )


def make_snapshot(samples=None, processes=None):
    now = datetime.now()
    samples = samples if samples is not None else []
    processes = processes if processes is not None else []
    peak = samples[-1] if samples else make_sample(cpu=0.0, ram=0.0, resp=0.0)
    return LagSnapshot(
        id=1,
        event_id=1,
        captured_at=now,
        pre_lag_samples=samples,
        peak_sample=peak,
        top_processes=processes,
        peak_cpu=peak.cpu_percent,
        peak_ram=peak.ram_percent,
        peak_responsiveness_ms=peak.responsiveness_ms,
    )


def make_event(category="CPU_BOUND", cause="cause text", frame_summary="", duration=2.0):
    now = datetime.now()
    return LagEvent(
        id=1,
        started_at=now,
        ended_at=now + timedelta(seconds=duration),
        peak_composite_score=0.8,
        cause=cause,
        cause_code=category,
        category=category,
        scope="LOCAL",
        duration_seconds=duration,
        frame_summary=frame_summary,
    )


# ---------------------------------------------------------------------------
# DetectionEngine tests
# ---------------------------------------------------------------------------

class TestDetectionEngine:

    def test_no_lag_on_idle_system(self):
        engine = DetectionEngine()
        for _ in range(15):
            engine.ingest(make_sample(cpu=5, ram=30, resp=8))
        assert not engine._in_lag

    def test_lag_state_on_sustained_high_cpu(self):
        """After 2+ consecutive high-score samples the engine should mark lag."""
        engine = DetectionEngine()
        for _ in range(10):
            engine.ingest(make_sample(cpu=5, ram=30, resp=8))
        for _ in range(5):
            engine.ingest(make_sample(cpu=95, ram=30, resp=8))
        assert engine._in_lag or engine._peak_score > 0, \
            "High sustained CPU should trigger lag state"

    def test_single_sample_spike_does_not_trigger(self):
        """A single bad sample should NOT fire a lag event."""
        engine = DetectionEngine()
        for _ in range(10):
            engine.ingest(make_sample(cpu=5, ram=30, resp=8))
        engine.ingest(make_sample(cpu=99, ram=30, resp=8))
        for _ in range(3):
            engine.ingest(make_sample(cpu=5, ram=30, resp=8))
        # After recovery a single spike should have been cleared
        assert not engine._in_lag

    def test_responsiveness_spike_triggers_lag_state(self):
        """High responsiveness alone should push composite above threshold."""
        engine = DetectionEngine()
        for _ in range(10):
            engine.ingest(make_sample(cpu=5, ram=30, resp=8))
        for _ in range(5):
            engine.ingest(make_sample(cpu=15, ram=35, resp=300))
        assert engine._in_lag or engine._peak_score > 0

    def test_score_emitted_every_sample(self):
        engine = DetectionEngine()
        scores = []
        engine.score_updated.connect(lambda s: scores.append(s))
        for _ in range(5):
            engine.ingest(make_sample())
        assert len(scores) == 5

    def test_composite_score_range(self):
        engine = DetectionEngine()
        scores = []
        engine.score_updated.connect(lambda s: scores.append(s))
        for cpu in [0, 25, 50, 75, 100]:
            engine.ingest(make_sample(cpu=cpu, ram=cpu * 0.8, resp=cpu * 0.5))
        for s in scores:
            assert 0.0 <= s.composite <= 1.0

    def test_recovery_resets_consecutive_count(self):
        """Consecutive lag counter should reset after calm samples."""
        engine = DetectionEngine()
        for _ in range(5):
            engine.ingest(make_sample(cpu=95, ram=40, resp=8))
        for _ in range(5):
            engine.ingest(make_sample(cpu=5, ram=30, resp=8))
        assert not engine._in_lag
        assert engine._consecutive_lag == 0

    def test_idle_baseline_does_not_flag_normal_load(self):
        """
        Regression: a quiet machine learns mean=8%, σ=1, which used to put the
        learned lag line at 10% — and then an ordinary game load (30–50%) read
        as sustained lag. The threshold floor must keep the line at a level no
        healthy machine sits below.
        """
        engine = DetectionEngine()
        for _ in range(70):
            engine.ingest(make_sample(cpu=8, ram=35, resp=10))
        assert engine.baseline.is_ready
        for _ in range(10):
            engine.ingest(make_sample(cpu=45, ram=50, resp=14))
        assert not engine._in_lag

    def test_sustained_real_saturation_still_triggers(self):
        """The floor must not swallow genuine 95%+ saturation."""
        engine = DetectionEngine()
        for _ in range(70):
            engine.ingest(make_sample(cpu=8, ram=35, resp=10))
        for _ in range(5):
            engine.ingest(make_sample(cpu=97, ram=60, resp=30))
        assert engine._in_lag


# ---------------------------------------------------------------------------
# CauseAnalyzer tests
# ---------------------------------------------------------------------------

class TestCauseAnalyzer:
    """
    These were written against an older analyze() that returned
    (cause_code, message) and codes like CPU_SPIKE / RAM_EXHAUSTION. It now
    returns (category, message, scope) with the CATEGORY_* names, so the asserts
    reference those constants directly rather than re-hardcoding the strings.
    """

    def test_cpu_spike_detected(self):
        set_cpu_count(8)
        analyzer = CauseAnalyzer()
        # 900% per-core on 8 threads = 112% of the machine: genuine saturation.
        proc = make_process("chrome.exe", cpu=900.0)
        sample = make_sample(cpu=95, processes=[proc])
        category, msg, scope = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_CPU_BOUND
        assert "chrome.exe" in msg
        assert scope == analyzer_mod.SCOPE_LOCAL

    def test_game_at_200pct_on_32_threads_is_not_a_cpu_spike(self):
        """
        Regression for the false positive that drove this rework: a 32-thread
        machine running a game at 200% per-core CPU (= 6% of the machine) was
        reported every few seconds as "game.exe is starving the system".
        """
        set_cpu_count(32)
        analyzer = CauseAnalyzer()
        proc = make_process("game.exe", cpu=200.0)
        sample = make_sample(cpu=25, ram=50, resp=12, processes=[proc])
        category, msg, scope = analyzer.analyze(sample, [])
        assert category != analyzer_mod.CATEGORY_CPU_BOUND
        assert "game.exe" not in msg or "starve" not in msg

    def test_tracked_game_gets_higher_exempt_line_than_others(self):
        set_cpu_count(8)
        analyzer = CauseAnalyzer()
        game = make_process("game.exe", cpu=400.0)          # 50% of machine
        target = make_target(cpu=400.0)
        sample = make_sample(cpu=55, processes=[game], target=target)
        category, _, _ = analyzer.analyze(sample, [])
        assert category != analyzer_mod.CATEGORY_CPU_BOUND

        # Same share by a NON-tracked process is above the 40% line and not
        # exempt, so it is reported.
        other = make_process("compile.exe", cpu=400.0)
        sample = make_sample(cpu=55, processes=[other])
        category, msg, _ = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_CPU_BOUND
        assert "compile.exe" in msg

    def test_ram_exhaustion_detected(self):
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=20, ram=92, resp=15, swap=15.0)
        category, msg, scope = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_SYSTEM_RAM_PRESSURE
        assert "swap" in msg.lower()

    def test_disk_io_detected(self):
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=20, ram=40, resp=200)
        category, msg, scope = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_IO_STALL

    def test_background_cluster_detected(self):
        set_cpu_count(8)
        analyzer = CauseAnalyzer()
        # PIDs start at 1000: PID 0 is the kernel idle process and is now filtered
        # out of the "who is eating the CPU" ranking, so it cannot stand in for a
        # fake background service any more. Each member must hold ≥5% of the
        # whole machine (40% per-core on 8 threads).
        procs = [make_process(f"service{i}.exe", cpu=60.0, pid=1000 + i) for i in range(7)]
        sample = make_sample(cpu=90, processes=procs)
        category, msg, scope = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_BACKGROUND_INTERFERENCE
        assert "service0.exe" in msg

    def test_small_cluster_below_member_share_not_reported(self):
        """Seven processes at 20% per-core each (2.5% of an 8-thread machine)
        are a normal background hum, not interference."""
        set_cpu_count(8)
        analyzer = CauseAnalyzer()
        procs = [make_process(f"service{i}.exe", cpu=20.0, pid=1000 + i) for i in range(7)]
        sample = make_sample(cpu=30, processes=procs)
        category, msg, scope = analyzer.analyze(sample, [])
        assert category != analyzer_mod.CATEGORY_BACKGROUND_INTERFERENCE

    def test_fallback_returns_valid_code(self):
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=70, ram=60, resp=30)
        category, msg, scope = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_LOCAL_STUTTER
        assert len(msg) > 10

    def test_explanation_is_human_readable(self):
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=50, ram=50, resp=10)
        category, msg, scope = analyzer.analyze(sample, [])
        assert isinstance(msg, str)
        assert len(msg) > 20

    def test_weak_signals_stay_undetermined(self):
        """A calm system must not be handed a confident local root cause."""
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=15, ram=50, resp=10)
        category, msg, scope = analyzer.analyze(sample, [])
        assert category == analyzer_mod.CATEGORY_UNDETERMINED
        assert scope == analyzer_mod.SCOPE_UNDETERMINED

    def test_every_category_has_report_labels(self):
        """
        Frame events can now carry any system category, so a missing label would
        surface in the UI as a raw code with no icon or colour.
        """
        from ui.detail_panel import CAUSE_COLOURS, CAUSE_ICONS, CAUSE_LABELS_ZH

        categories = [
            getattr(analyzer_mod, name)
            for name in dir(analyzer_mod)
            if name.startswith("CATEGORY_")
        ]
        for category in categories:
            assert category in CAUSE_LABELS_ZH, category
            assert category in CAUSE_ICONS, category
            assert category in CAUSE_COLOURS, category


# ---------------------------------------------------------------------------
# Frame episode analysis
# ---------------------------------------------------------------------------

class TestFrameEpisodeAnalysis:
    """
    Frame events used to report only frame timings. These pin down that the
    system rules now contribute a root cause, without letting them overwrite a
    frame-side verdict that carries more information.
    """

    def test_specific_system_verdict_wins(self):
        set_cpu_count(8)
        analyzer = CauseAnalyzer()
        proc = make_process("chrome.exe", cpu=900.0)
        sample = make_sample(cpu=95, processes=[proc])
        result = analyzer.analyze_frame_episode(make_episode(), sample, [])
        assert result.category == "CPU_BOUND"
        assert result.used_system_cause
        assert "chrome.exe" in result.explanation
        # Both halves of the story survive: cause and what the player saw.
        assert "Peak frame time" in result.explanation

    def test_weak_system_verdict_yields_to_detector(self):
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=10, ram=30, resp=8)
        episode = make_episode(category="GPU_BOUND", scope="LOCAL")
        result = analyzer.analyze_frame_episode(episode, sample, [])
        assert result.category == "GPU_BOUND"
        assert not result.used_system_cause
        assert result.scope == "LOCAL"
        # The weak system text is still carried as supporting context.
        assert result.system_cause

    def test_missing_peak_sample_falls_back_to_detector(self):
        analyzer = CauseAnalyzer()
        episode = make_episode(category="GPU_BOUND", explanation="GPU was saturated.")
        result = analyzer.analyze_frame_episode(episode, None, [])
        assert result.category == "GPU_BOUND"
        assert result.explanation == "GPU was saturated."
        assert result.system_cause == ""
        assert not result.used_system_cause

    def test_missing_peak_sample_without_detector_category(self):
        analyzer = CauseAnalyzer()
        result = analyzer.analyze_frame_episode(make_episode(), None, [])
        assert result.category == "LOCAL_STUTTER"
        assert result.scope == "LOCAL"

    def test_compat_mode_wording_differs(self):
        analyzer = CauseAnalyzer()
        compat = analyzer.summarize_frame_episode(
            make_episode(present_mode="compatibility", peak_cpu_wait_ms=30.0)
        )
        frame = analyzer.summarize_frame_episode(
            make_episode(present_mode="hardware", peak_cpu_wait_ms=30.0)
        )
        assert compat.startswith("Peak response delay")
        assert frame.startswith("Peak frame time")
        # Compatibility mode has no frame breakdown to report, so it must not
        # claim CPU wait / GPU busy numbers it never measured.
        assert "CPU wait" not in compat
        assert "CPU wait" in frame

    def test_summary_reports_freeze_over_slow_counts(self):
        analyzer = CauseAnalyzer()
        summary = analyzer.summarize_frame_episode(
            make_episode(freeze_frame_count=4, slow_frame_count=9)
        )
        assert "4 freeze-level samples" in summary
        assert "slow samples" not in summary

    def test_frame_summary_always_populated(self):
        set_cpu_count(8)
        analyzer = CauseAnalyzer()
        sample = make_sample(cpu=95, processes=[make_process("chrome.exe", cpu=900.0)])
        for peak in (sample, None):
            result = analyzer.analyze_frame_episode(make_episode(), peak, [])
            assert "180.0 ms" in result.frame_summary


# ---------------------------------------------------------------------------
# Frame drop clustering
# ---------------------------------------------------------------------------

def make_frame(frame_ms=8.0, displayed=True, ts_offset=0.0):
    return FrameSample(
        timestamp=datetime.now() + timedelta(milliseconds=ts_offset),
        process_name="game.exe",
        process_id=1234,
        swap_chain="0",
        runtime="DXGI",
        present_mode="Hardware: Legacy Flip",
        sync_interval=0,
        allows_tearing=True,
        frame_time_ms=frame_ms,
        displayed_time_ms=frame_ms if displayed else 0.0,
        was_displayed=displayed,
    )


class TestFrameDropClustering:
    """
    One dropped frame is a 4 ms longer gap at 240 Hz — invisible on its own.
    Its visible consequence is caught by the display-gap thresholds; the drop
    itself only becomes an event when frames drop as a burst.
    """

    def _detector(self):
        from core.frame_detector import FrameStutterDetector
        return FrameStutterDetector()

    def test_single_drop_does_not_trigger(self):
        det = self._detector()
        for i in range(30):
            det.ingest_frame(make_frame(ts_offset=i * 50))
        det.ingest_frame(make_frame(displayed=False, ts_offset=30 * 50))
        det.ingest_frame(make_frame(ts_offset=31 * 50))
        det.ingest_frame(make_frame(ts_offset=32 * 50))
        assert not det._active

    def test_drop_burst_triggers(self):
        det = self._detector()
        for i in range(30):
            det.ingest_frame(make_frame(ts_offset=i * 50))
        # Frame 1: recent_drops=1 → no event. Frame 2: recent_drops=2 → no
        # event. Frame 3: recent_drops=3 → event; this frame is absorbed.
        det.ingest_frame(make_frame(displayed=False, ts_offset=30 * 50))
        det.ingest_frame(make_frame(displayed=False, ts_offset=31 * 50))
        assert not det._active
        det.ingest_frame(make_frame(displayed=False, ts_offset=32 * 50))
        assert det._active
        assert det._dropped_frames >= 1


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

class TestDetailPanelReport:
    """
    The Chinese report rebuilds cause text from the snapshot rather than using
    event.cause verbatim, so it has two ways to lose information: dropping the
    frame timings, and quoting the recorder's zero-filled placeholder as if it
    had been measured. These pin both down.
    """

    SUMMARY = "Peak frame time reached 180.0 ms (avg 40.0 ms, P95 90.0 ms). 4 freeze-level samples."
    COMPAT_SUMMARY = "Peak response delay reached 950.0 ms (avg 220.0 ms, P95 700.0 ms). 12 slow samples."

    def _panel(self, language="zh"):
        panel = DetailPanelWidget()
        panel._report_language = language
        return panel

    def test_zh_report_keeps_frame_timings(self):
        panel = self._panel()
        proc = make_process("chrome.exe", cpu=75.0)
        snapshot = make_snapshot([make_sample(cpu=80, processes=[proc])], [proc])
        event = make_event(frame_summary=self.SUMMARY)
        text = panel._explanation_text(event, snapshot)
        assert "chrome.exe" in text            # why it happened
        assert "峰值帧时间 180.0 ms" in text     # what the player saw
        assert "4 个采样达到冻结级别" in text

    def test_zh_report_uses_response_wording_for_compat(self):
        panel = self._panel()
        proc = make_process("game.exe", cpu=75.0)
        snapshot = make_snapshot([make_sample(cpu=80, processes=[proc])], [proc])
        event = make_event(frame_summary=self.COMPAT_SUMMARY)
        text = panel._explanation_text(event, snapshot)
        assert "峰值响应延迟 950.0 ms" in text
        assert "峰值帧时间" not in text
        assert "12 个采样偏慢" in text

    def test_zh_report_without_frame_summary_is_unchanged(self):
        panel = self._panel()
        proc = make_process("chrome.exe", cpu=75.0)
        snapshot = make_snapshot([make_sample(cpu=80, processes=[proc])], [proc])
        text = panel._explanation_text(make_event(), snapshot)
        assert "chrome.exe" in text
        assert "玩家侧表现" not in text

    def test_placeholder_snapshot_does_not_invent_metrics(self):
        panel = self._panel()
        event = make_event(category="LOCAL_STUTTER", frame_summary=self.SUMMARY)
        text = panel._explanation_text(event, make_snapshot([], []))
        # The placeholder's zeros must never be presented as readings.
        assert "峰值 CPU 0%" not in text
        assert "没有采集到配套的系统快照" in text
        assert "峰值帧时间 180.0 ms" in text

    def test_placeholder_snapshot_hides_peak_metrics_block(self):
        panel = self._panel()
        event = make_event(category="LOCAL_STUTTER", frame_summary=self.SUMMARY)
        panel.show_event(event, make_snapshot([], []))
        assert "峰值指标" not in panel._browser.toHtml()

        proc = make_process("chrome.exe", cpu=75.0)
        panel.show_event(event, make_snapshot([make_sample(cpu=80, processes=[proc])], [proc]))
        assert "峰值指标" in panel._browser.toHtml()

    def test_en_report_does_not_duplicate_summary(self):
        panel = self._panel("en")
        snapshot = make_snapshot([make_sample(cpu=80)], [])
        # The analyzer already folded the summary into `cause` here.
        event = make_event(cause=f"chrome.exe ate the CPU. {self.SUMMARY}", frame_summary=self.SUMMARY)
        text = panel._explanation_text(event, snapshot)
        assert text.count("Peak frame time reached") == 1

    def test_en_report_appends_missing_summary(self):
        panel = self._panel("en")
        snapshot = make_snapshot([make_sample(cpu=80)], [])
        event = make_event(cause="GPU was saturated.", frame_summary=self.SUMMARY)
        text = panel._explanation_text(event, snapshot)
        assert "GPU was saturated." in text
        assert "Peak frame time reached 180.0 ms" in text

    def test_pending_event_reports_progress_not_cause(self):
        panel = self._panel()
        event = make_event(category="REPORT_PENDING", frame_summary=self.SUMMARY)
        event.is_pending = True
        text = panel._explanation_text(event, make_snapshot([make_sample(cpu=80)], []))
        assert "仍在进行中" in text
        assert "玩家侧表现" not in text


# ---------------------------------------------------------------------------
# Sigmoid score helper
# ---------------------------------------------------------------------------

class TestSigmoidScore:

    def test_at_threshold_returns_half(self):
        from core.detection import DetectionEngine
        score = DetectionEngine._sigmoid_score(50.0, 50.0, steepness=0.1)
        assert abs(score - 0.5) < 0.01

    def test_well_below_threshold_near_zero(self):
        from core.detection import DetectionEngine
        score = DetectionEngine._sigmoid_score(10.0, 80.0, steepness=0.1)
        assert score < 0.1

    def test_well_above_threshold_near_one(self):
        from core.detection import DetectionEngine
        score = DetectionEngine._sigmoid_score(150.0, 80.0, steepness=0.1)
        assert score > 0.9


if __name__ == "__main__":
    import traceback
    suites = [
        TestDetectionEngine(),
        TestCauseAnalyzer(),
        TestFrameEpisodeAnalysis(),
        TestFrameDropClustering(),
        TestDetailPanelReport(),
        TestSigmoidScore(),
    ]
    passed = failed = 0
    for suite in suites:
        for name in sorted(dir(suite)):
            if name.startswith("test_"):
                try:
                    getattr(suite, name)()
                    print(f"  ✓  {suite.__class__.__name__}.{name}")
                    passed += 1
                except Exception as e:
                    print(f"  ✗  {suite.__class__.__name__}.{name}: {e}")
                    traceback.print_exc()
                    failed += 1
    print(f"\n{passed} passed, {failed} failed")
