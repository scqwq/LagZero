"""
core/analyzer.py — Rules engine that turns raw lag data into human-readable explanations.

Rule priority (first match wins):
  1. Single process >60% CPU          → CPU_SPIKE
  2. RAM >90% + swap active           → RAM_EXHAUSTION
  3. Many small processes (cluster)   → BACKGROUND_CLUSTER
  4. Responsiveness delay, CPU normal → DISK_IO (disk I/O bottleneck)
  5. Fallback                         → SCHEDULER_CONTENTION

Each rule returns a (cause_code, explanation) tuple.
Adding new rules is easy — just add a method prefixed with _rule_ and
register it in RULE_PRIORITY.  The engine tries them in order.
"""
from dataclasses import dataclass

from core.collectors import is_idle_pseudo_process
from core.models import FrameStutterEpisode, SystemSample, ProcessSample

CATEGORY_CPU_BOUND = "CPU_BOUND"
CATEGORY_GPU_BOUND = "GPU_BOUND"
CATEGORY_RAM_PRESSURE = "RAM_PRESSURE"
CATEGORY_SYSTEM_RAM_PRESSURE = "SYSTEM_RAM_PRESSURE"
CATEGORY_GAME_MEMORY_LIMIT = "GAME_MEMORY_LIMIT"
CATEGORY_VRAM_PRESSURE = "VRAM_PRESSURE"
CATEGORY_DRIVER_RENDER_PATH = "DRIVER_RENDER_PATH"
CATEGORY_IO_STALL = "IO_STALL"
# Frames were produced on time but did not reach the screen on time. Comes from
# the frame attribution rather than any system rule — no system counter can see
# this, which is why the class used to be missed entirely.
CATEGORY_DISPLAY_PIPELINE = "DISPLAY_PIPELINE"
CATEGORY_BACKGROUND_INTERFERENCE = "BACKGROUND_INTERFERENCE"
CATEGORY_LOCAL_STUTTER = "LOCAL_STUTTER"
CATEGORY_UNDETERMINED = "UNDETERMINED"

SCOPE_LOCAL = "LOCAL"
SCOPE_NETWORK = "NETWORK"
SCOPE_UNDETERMINED = "UNDETERMINED"

# Thresholds used by rules
SINGLE_PROC_CPU_THRESHOLD = 40.0    # % — a single process eating this much is suspicious
RAM_EXHAUSTION_THRESHOLD = 88.0     # % RAM used
SWAP_ACTIVE_THRESHOLD = 5.0         # % swap used
BACKGROUND_CLUSTER_COUNT = 5        # number of processes each contributing ≥5% CPU
CPU_NORMAL_FOR_DISK = 55.0          # if CPU below this but lag detected → disk
RESP_HIGH_THRESHOLD_MS = 40.0       # ms — above this is "high responsiveness delay"
VRAM_PRESSURE_RATIO = 0.92
TARGET_MEMORY_LIMIT_MIN_MB = 1024.0

# Verdicts that carry no actionable root cause. When the system rules land here
# but the frame detector reached something specific (e.g. GPU_BOUND from frame
# telemetry), the frame-side answer is the better one to show.
WEAK_CATEGORIES = frozenset({CATEGORY_UNDETERMINED, CATEGORY_LOCAL_STUTTER})


@dataclass
class FrameCauseResult:
    """
    Cause verdict for a frame/response stutter episode.

    Separate from the plain 3-tuple because a frame event carries both halves of
    the story: what the player saw (frame timings) and why it happened (system
    rules). Collapsing them into one string loses the ability to render or store
    them independently.
    """
    category: str
    explanation: str
    scope: str
    frame_summary: str = ""
    system_cause: str = ""
    # True when the system rules supplied the category, False when the frame
    # detector's own classification was kept.
    used_system_cause: bool = False


class CauseAnalyzer:

    def analyze(self, peak_sample: SystemSample, pre_lag_samples: list[SystemSample]) -> tuple[str, str, str]:
        """
        Returns (category, human_readable_explanation, scope).

        peak_sample     — the SystemSample at peak lag
        pre_lag_samples — the 5 samples leading up to the lag event
        """
        for rule_fn in self._rules():
            result = rule_fn(peak_sample, pre_lag_samples)
            if result:
                return result
        return CATEGORY_UNDETERMINED, (
            "No clear local bottleneck was identified. The stutter is real, but the current data is not enough "
            "to confidently separate CPU, RAM, disk, background load, or a possible network-side issue."
        ), SCOPE_UNDETERMINED

    def analyze_frame_episode(
        self,
        episode: FrameStutterEpisode,
        peak_sample: SystemSample | None,
        pre_lag_samples: list[SystemSample],
    ) -> FrameCauseResult:
        """
        Explain a frame/response stutter using the surrounding system snapshot.

        Frame events used to bypass this class entirely: the report said "peak
        frame time 180 ms" and stopped, even though the recorder had already
        captured which process was eating the CPU at that moment. This runs the
        same rules the system path uses, then keeps the frame timings alongside
        the cause so the report can state both what the player saw and why.

        The detector's own category is kept as a fallback, and deliberately wins
        over a weak system verdict — GPU_BOUND inferred from frame telemetry is
        better evidence than the analyzer's catch-all.
        """
        detector_category = (episode.category or "").strip()
        detector_scope = (episode.scope or "").strip()

        if peak_sample is None:
            # No system context at all: the detector's own reading is all there is.
            return FrameCauseResult(
                category=detector_category or CATEGORY_LOCAL_STUTTER,
                explanation=episode.explanation,
                scope=detector_scope or SCOPE_LOCAL,
                frame_summary=self.summarize_frame_episode(episode),
                system_cause="",
                used_system_cause=False,
            )

        system_category, system_cause, system_scope = self.analyze(peak_sample, pre_lag_samples)
        frame_summary = self.summarize_frame_episode(episode)

        # A concrete system finding (a named process, RAM exhaustion, VRAM
        # pressure) explains the stutter better than the frame timings alone.
        # The weak verdicts below are exactly the cases where the frame-side
        # classification carries more information than the system rules.
        system_is_weak = system_category in WEAK_CATEGORIES
        detector_is_specific = bool(detector_category) and detector_category not in WEAK_CATEGORIES

        if system_is_weak and detector_is_specific:
            return FrameCauseResult(
                category=detector_category,
                explanation=f"{episode.explanation} {system_cause}".strip(),
                scope=detector_scope or system_scope,
                frame_summary=frame_summary,
                system_cause=system_cause,
                used_system_cause=False,
            )

        return FrameCauseResult(
            category=system_category,
            explanation=f"{system_cause} {frame_summary}".strip(),
            scope=system_scope if not detector_scope or system_scope != SCOPE_UNDETERMINED else detector_scope,
            frame_summary=frame_summary,
            system_cause=system_cause,
            used_system_cause=True,
        )

    @staticmethod
    def summarize_frame_episode(episode: FrameStutterEpisode) -> str:
        """
        One-line "what the player saw" summary.

        Compatibility mode measures window response delay rather than frame time,
        so the wording follows the source instead of calling both "frame time".
        """
        is_compat = episode.present_mode == "compatibility"
        label = "Peak response delay" if is_compat else "Peak frame time"
        parts = [
            f"{label} reached {episode.peak_frame_time_ms:.1f} ms "
            f"(avg {episode.avg_frame_time_ms:.1f} ms, P95 {episode.p95_frame_time_ms:.1f} ms)."
        ]
        # The learned norm is what makes the peak mean anything: 30 ms is a
        # non-event at 30 fps and a five-fold hitch at 240 Hz.
        if not is_compat and episode.baseline_frame_time_ms > 0.0:
            parts.append(
                f"Normal for this session was {episode.baseline_frame_time_ms:.1f} ms."
            )
        if episode.freeze_frame_count:
            parts.append(f"{episode.freeze_frame_count} freeze-level samples.")
        elif episode.slow_frame_count:
            parts.append(f"{episode.slow_frame_count} slow samples.")
        if not is_compat and episode.dropped_frame_count:
            parts.append(
                f"{episode.dropped_frame_count} frames never reached the screen."
            )
        if not is_compat and (episode.peak_cpu_wait_ms or episode.peak_gpu_busy_ms):
            parts.append(
                f"Peak CPU wait {episode.peak_cpu_wait_ms:.1f} ms, "
                f"peak GPU busy {episode.peak_gpu_busy_ms:.1f} ms."
            )
        # End-to-end input latency is only reported when PresentMon actually
        # correlated an input event with the frame — it is NA more often than not
        # (measured NA in 1837/1837 desktop-composition rows), so a 0.0 here must
        # never be printed as "zero latency".
        if not is_compat and episode.peak_input_latency_ms > 0.0:
            parts.append(
                f"Peak end-to-end input latency {episode.peak_input_latency_ms:.1f} ms."
            )
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Rules (tried in order — first match wins)
    # ------------------------------------------------------------------

    @staticmethod
    def _real_processes(processes: list[ProcessSample]) -> list[ProcessSample]:
        """
        Drop the kernel's idle bookkeeping entries before blaming anyone.

        "System Idle Process" (PID 0) measures the time the CPU spent doing
        nothing, so on a quiet machine it reports ~100% and used to be named as
        the process that "was consuming 95% CPU". The collector already filters
        it, but old snapshots read back from SQLite still contain it, so the rule
        filters again rather than trusting the stored data.
        """
        return [p for p in processes if not is_idle_pseudo_process(p.pid, p.name)]

    def _rules(self):
        return [
            self._rule_vram_pressure,
            self._rule_game_memory_limit,
            self._rule_single_cpu_spike,
            self._rule_ram_exhaustion,
            self._rule_background_cluster,
            self._rule_disk_io,
            self._rule_driver_render_path,
            self._rule_scheduler_contention,
        ]

    def _rule_vram_pressure(
        self, sample: SystemSample, _pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        gpu = sample.gpu_memory
        if gpu is None or gpu.local_budget_mb <= 0:
            return None
        if gpu.local_usage_ratio < VRAM_PRESSURE_RATIO:
            return None
        shared_hint = ""
        if gpu.shared_usage_mb >= 512 and gpu.shared_usage_ratio >= 0.15:
            shared_hint = (
                f" Shared GPU memory was also elevated at {gpu.shared_usage_mb:.0f} MB, "
                "which often means textures or render targets are spilling beyond local VRAM."
            )
        return (
            CATEGORY_VRAM_PRESSURE,
            f"GPU local memory usage reached {gpu.local_usage_mb:.0f} MB out of a {gpu.local_budget_mb:.0f} MB budget "
            f"({gpu.local_usage_ratio * 100:.0f}% used). This looks like VRAM pressure rather than a pure shader/compute bottleneck."
            f"{shared_hint}",
            SCOPE_LOCAL,
        )

    def _rule_game_memory_limit(
        self, sample: SystemSample, pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        target = sample.target_process
        if target is None or target.memory_mb < TARGET_MEMORY_LIMIT_MIN_MB:
            return None
        if sample.ram_percent >= 78.0:
            return None
        mem_series = [s.target_process.memory_mb for s in pre if s.target_process and s.target_process.pid == target.pid]
        if len(mem_series) < 3:
            return None
        mem_max = max(mem_series)
        mem_min = min(mem_series)
        if mem_max <= 0:
            return None
        spread_ratio = (mem_max - mem_min) / mem_max
        cpu_series = [s.target_process.cpu_percent for s in pre if s.target_process and s.target_process.pid == target.pid]
        avg_target_cpu = sum(cpu_series) / len(cpu_series) if cpu_series else target.cpu_percent
        if spread_ratio <= 0.12 and avg_target_cpu >= 20.0:
            return (
                CATEGORY_GAME_MEMORY_LIMIT,
                f'The tracked game process "{target.name}" stayed near a narrow memory ceiling around '
                f"{mem_max:.0f} MB even though total system RAM was not full ({sample.ram_percent:.0f}% used). "
                "That pattern often means the game, runtime, or launch settings are limiting how much memory the game is actually using.",
                SCOPE_LOCAL,
            )
        return None

    def _rule_single_cpu_spike(
        self, sample: SystemSample, _pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        """One process is hogging the CPU."""
        candidates = self._real_processes(sample.top_processes)
        if not candidates:
            return None
        top = candidates[0]
        if top.cpu_percent >= SINGLE_PROC_CPU_THRESHOLD:
            pct = round(top.cpu_percent, 1)
            return (
                CATEGORY_CPU_BOUND,
                f'"{top.name}" (PID {top.pid}) was consuming {pct}% CPU, '
                f"causing the system to become unresponsive. "
                f"Try closing or restarting this application.",
                SCOPE_LOCAL,
            )
        return None

    def _rule_ram_exhaustion(
        self, sample: SystemSample, _pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        """RAM is nearly full and the OS is paging to disk."""
        if sample.ram_percent >= RAM_EXHAUSTION_THRESHOLD and sample.swap_percent >= SWAP_ACTIVE_THRESHOLD:
            used_gb = round(sample.ram_used_mb / 1024, 1)
            total_gb = round(sample.ram_total_mb / 1024, 1)
            swap = round(sample.swap_percent, 1)
            return (
                CATEGORY_SYSTEM_RAM_PRESSURE,
                f"System RAM is critically full ({used_gb} GB / {total_gb} GB used, "
                f"{swap}% swap active). The OS is writing memory to disk (paging), "
                f"which is much slower than RAM. Close unused applications or browser tabs.",
                SCOPE_LOCAL,
            )
        if sample.ram_percent >= RAM_EXHAUSTION_THRESHOLD:
            used_gb = round(sample.ram_used_mb / 1024, 1)
            total_gb = round(sample.ram_total_mb / 1024, 1)
            return (
                CATEGORY_SYSTEM_RAM_PRESSURE,
                f"RAM usage is very high ({used_gb} GB / {total_gb} GB). "
                f"The system is running out of memory headroom. "
                f"Close unused applications to free memory.",
                SCOPE_LOCAL,
            )
        return None

    def _rule_driver_render_path(
        self, sample: SystemSample, pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        target = sample.target_process
        gpu = sample.gpu_memory
        if sample.cpu_percent >= 55 or sample.ram_percent >= 85:
            return None
        if sample.responsiveness_ms < 18.0:
            return None
        if target is not None and target.cpu_percent >= 55.0:
            return None
        if gpu is not None and gpu.local_usage_ratio >= 0.85:
            return None
        rising_resp = self.pre_lag_trend(pre, "responsiveness_ms") == "rising"
        if rising_resp:
            return (
                CATEGORY_DRIVER_RENDER_PATH,
                "Local stutter was visible, but CPU, system RAM, and VRAM pressure were not dominant. "
                "This pattern can happen with driver issues, compositor/render-path instability, overlays, or capture conflicts.",
                SCOPE_UNDETERMINED,
            )
        return None

    def _rule_background_cluster(
        self, sample: SystemSample, _pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        """Many small background processes adding up to a lot of CPU."""
        contributors = [
            p for p in self._real_processes(sample.top_processes) if p.cpu_percent >= 5.0
        ]
        if len(contributors) >= BACKGROUND_CLUSTER_COUNT:
            names = ", ".join(f'"{p.name}"' for p in contributors[:5])
            total = round(sum(p.cpu_percent for p in contributors), 1)
            return (
                CATEGORY_BACKGROUND_INTERFERENCE,
                f"{len(contributors)} background processes ({names}) are each consuming CPU, "
                f"totalling ~{total}% combined. No single villain, but the crowd is the problem. "
                f"Consider disabling startup applications.",
                SCOPE_LOCAL,
            )
        return None

    def _rule_disk_io(
        self, sample: SystemSample, _pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        """High responsiveness delay but CPU is normal → likely disk I/O bottleneck."""
        if (
            sample.responsiveness_ms >= RESP_HIGH_THRESHOLD_MS
            and sample.cpu_percent < CPU_NORMAL_FOR_DISK
        ):
            resp = round(sample.responsiveness_ms, 1)
            return (
                CATEGORY_IO_STALL,
                f"CPU usage was normal ({round(sample.cpu_percent, 1)}%) but system responsiveness "
                f"was severely degraded ({resp} ms delay). This typically means a disk I/O bottleneck — "
                f"something is reading/writing heavily to storage (antivirus scan, updates, backup, "
                f"or a failing drive).",
                SCOPE_LOCAL,
            )
        return None

    def _rule_scheduler_contention(
        self, sample: SystemSample, _pre: list[SystemSample]
    ) -> tuple[str, str, str] | None:
        """Fallback — general system stress, no single root cause."""
        resp = round(sample.responsiveness_ms, 1)
        cpu = round(sample.cpu_percent, 1)
        if (
            sample.cpu_percent < 40.0
            and sample.ram_percent < 75.0
            and sample.responsiveness_ms < 20.0
        ):
            return (
                CATEGORY_UNDETERMINED,
                "The game felt bad, but the current local signals are weak: CPU, RAM, and responsiveness did not show a strong local bottleneck. "
                "This does not prove a network problem, but it means a remote/network factor is still plausible.",
                SCOPE_UNDETERMINED,
            )
        return (
            CATEGORY_LOCAL_STUTTER,
            f"System was under general stress (CPU: {cpu}%, responsiveness: {resp} ms) "
            f"but no single clear cause was identified. This may be OS scheduler contention — "
            f"many processes competing for CPU time simultaneously.",
            SCOPE_LOCAL,
        )

    # ------------------------------------------------------------------
    # Pre-lag trend helpers (used for richer explanations in future phases)
    # ------------------------------------------------------------------

    @staticmethod
    def pre_lag_trend(pre_samples: list[SystemSample], attribute: str) -> str:
        """Returns 'rising', 'falling', or 'stable' for a given attribute over pre-lag samples."""
        if len(pre_samples) < 2:
            return "stable"
        values = [getattr(s, attribute) for s in pre_samples]
        delta = values[-1] - values[0]
        if delta > 10:
            return "rising"
        if delta < -10:
            return "falling"
        return "stable"
