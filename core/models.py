"""
core/models.py — Data models for lag events, snapshots, process samples,
game sessions, and frame telemetry.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ProcessSample:
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    status: str = "running"
    cpu_machine_share: float = 0.0


@dataclass
class ProcessGroupSample:
    """Aggregated resource usage for processes that must be viewed as a group."""
    name: str
    process_count: int
    cpu_machine_share: float
    memory_mb: float


@dataclass
class TargetProcessMetrics:
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    private_memory_mb: float = 0.0
    read_kb_s: float = 0.0
    write_kb_s: float = 0.0
    thread_count: int = 0
    cpu_machine_share: float = 0.0


@dataclass
class GpuMemorySnapshot:
    local_usage_mb: float
    local_budget_mb: float
    shared_usage_mb: float = 0.0
    shared_budget_mb: float = 0.0
    local_usage_ratio: float = 0.0
    shared_usage_ratio: float = 0.0


@dataclass
class SystemSample:
    """A single 1-second snapshot of system state."""
    timestamp: datetime
    cpu_percent: float          # overall CPU %
    cpu_per_core: list[float]   # per-core %
    ram_percent: float          # RAM usage %
    ram_used_mb: float
    ram_total_mb: float
    swap_percent: float
    responsiveness_ms: float    # how long a simple OS op took (ms)
    ram_available_mb: float = 0.0
    top_processes: list[ProcessSample] = field(default_factory=list)
    process_groups: list[ProcessGroupSample] = field(default_factory=list)
    target_process: Optional[TargetProcessMetrics] = None
    gpu_memory: Optional[GpuMemorySnapshot] = None


@dataclass
class LagScore:
    """Composite score for a single sample."""
    timestamp: datetime
    cpu_score: float        # 0–1
    ram_score: float        # 0–1
    responsiveness_score: float  # 0–1
    composite: float        # weighted average
    is_lag: bool = False


@dataclass
class LagEvent:
    """A confirmed lag event — persisted to SQLite."""
    id: Optional[int]
    started_at: datetime
    ended_at: Optional[datetime]
    peak_composite_score: float
    cause: str                  # human-readable explanation
    cause_code: str             # machine tag: CPU_SPIKE, RAM_EXHAUSTION, etc.
    category: str = ""
    scope: str = ""
    duration_seconds: float = 0.0
    snapshot_id: Optional[int] = None
    is_pending: bool = False
    # Compact, language-neutral frame/response timing line for events that came
    # from a stutter detector. The system snapshot has no frame data of its own,
    # so without this the report loses "what the player saw" as soon as the cause
    # analyzer supplies "why it happened".
    frame_summary: str = ""
    # Which bucket produced this event: "frame" / "compat" (confirmed stutter),
    # "minor" (short-lived or weak-evidence interference), "compat_pressure"
    # / "system" / "pressure" (resource-pressure oriented paths).
    detection_source: str = ""


@dataclass
class LagSnapshot:
    """Full capture of system state around a lag event."""
    id: Optional[int]
    event_id: Optional[int]
    captured_at: datetime
    pre_lag_samples: list[SystemSample]  # 5s before
    peak_sample: SystemSample
    top_processes: list[ProcessSample]
    peak_cpu: float
    peak_ram: float
    peak_responsiveness_ms: float
    process_groups: list[ProcessGroupSample] = field(default_factory=list)


@dataclass
class Baseline:
    """Learned normal behaviour for this machine."""
    cpu_mean: float = 10.0
    cpu_std: float = 5.0
    ram_mean: float = 40.0
    ram_std: float = 10.0
    responsiveness_mean_ms: float = 15.0
    responsiveness_std_ms: float = 5.0
    sample_count: int = 0
    is_ready: bool = False          # True after enough samples collected


@dataclass
class GameWindowCandidate:
    hwnd: int
    pid: int
    process_name: str
    title: str
    width: int
    height: int
    is_foreground: bool = False


@dataclass
class GameSessionInfo:
    pid: int | None
    process_name: str
    window_title: str
    hwnd: int | None = None
    width: int = 0
    height: int = 0
    is_foreground: bool = False
    source: str = "auto"

    @property
    def is_valid(self) -> bool:
        return bool(self.pid and self.process_name)


@dataclass
class FrameSample:
    """
    One presented frame.

    Field names follow PresentMon's v2 metric set, which separates work from
    waiting (CPUBusy vs CPUWait, GPUBusy vs GPUWait). The older v1 set is still
    parsed, but its columns are mapped onto these names so that everything
    downstream reasons about one vocabulary.
    """
    timestamp: datetime
    process_name: str
    process_id: int
    swap_chain: str
    runtime: str
    present_mode: str
    sync_interval: int
    allows_tearing: bool
    frame_time_ms: float
    # CPU side: time the frame's work occupied the CPU vs time spent blocked.
    cpu_busy_ms: float = 0.0
    cpu_wait_ms: float = 0.0
    # GPU side: gpu_time is total queue occupancy, gpu_busy excludes GPU idle
    # gaps within it, gpu_wait is the idle remainder.
    gpu_busy_ms: float = 0.0
    gpu_wait_ms: float = 0.0
    gpu_time_ms: float = 0.0
    gpu_latency_ms: float = 0.0
    # Display side. `displayed_time_ms` is the interval this frame remained on
    # screen; `was_displayed` is False when the frame never reached the screen
    # at all (v1 "Dropped", v2 "DisplayedTime == NA"). A dropped frame has no
    # meaningful displayed time, so the flag must be checked before the number.
    displayed_time_ms: float = 0.0
    display_latency_ms: float = 0.0
    was_displayed: bool = True
    # Animation smoothness: how far this frame's presentation drifted from where
    # a perfectly paced frame would have landed.
    animation_error_ms: float = 0.0
    has_animation_error: bool = False
    # End-to-end input latency. Frequently unavailable (PresentMon reports NA
    # unless it can correlate an input event with the frame), hence the flag
    # rather than a 0.0 that would read as "zero latency".
    input_latency_ms: float = 0.0
    click_latency_ms: float = 0.0
    flip_delay_ms: float = 0.0
    # Wall-clock seconds since capture start, straight from PresentMon. Used to
    # timestamp frames by when they happened rather than when we parsed them.
    capture_time_s: float = 0.0
    present_flags: int = 0
    metrics_version: str = "v2"
    raw_fields: dict[str, str] = field(default_factory=dict)

    @property
    def has_input_latency(self) -> bool:
        return self.input_latency_ms > 0.0


@dataclass
class FrameMetricsSnapshot:
    """Rolling summary for the live data panel."""
    updated_at: datetime
    target_process: str
    process_id: int
    sample_count: int
    fps: float
    avg_frame_time_ms: float
    p95_frame_time_ms: float
    max_frame_time_ms: float
    cpu_busy_ms: float
    cpu_wait_ms: float
    gpu_busy_ms: float
    present_mode: str
    # Rolling medians are what the attribution logic reasons about; the panel
    # only shows a couple of these, but keeping them here means the panel and
    # the report are reading the same numbers instead of two separate estimates.
    median_frame_time_ms: float = 0.0
    median_cpu_busy_ms: float = 0.0
    median_gpu_busy_ms: float = 0.0
    gpu_wait_ms: float = 0.0
    dropped_frame_count: int = 0
    input_latency_ms: float = 0.0
    metrics_version: str = "v2"


@dataclass
class FrameAttribution:
    """
    Which stage of the frame pipeline was responsible, and how sure we are.

    Kept as a dataclass rather than a bare category string because the report has
    to justify itself: "GPU_BOUND" alone is an assertion, while the evidence
    lines plus a confidence let a player see why, and let a weak verdict be
    recognised as weak instead of being stated with false authority.
    """
    category: str
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    # Per-stage share of the frame budget at the worst moment, 0-1.
    cpu_share: float = 0.0
    gpu_share: float = 0.0
    display_share: float = 0.0
    # Time blocked in the present path that GPU load could not account for.
    wait_share: float = 0.0

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.5


@dataclass
class FrameStutterEpisode:
    started_at: datetime
    ended_at: datetime
    target_process: str
    event_type: str
    peak_frame_time_ms: float
    avg_frame_time_ms: float
    p95_frame_time_ms: float
    slow_frame_count: int
    freeze_frame_count: int
    peak_cpu_wait_ms: float
    peak_gpu_busy_ms: float
    present_mode: str
    severity: float
    explanation: str
    category: str = ""
    scope: str = ""
    # Baseline context: what this game normally runs at, so the report can say
    # "30 ms against a 4 ms norm" instead of comparing against a fixed constant
    # that means nothing to a high-refresh-rate player.
    baseline_frame_time_ms: float = 0.0
    stutter_threshold_ms: float = 0.0
    # Frames that were presented but never reached the screen.
    dropped_frame_count: int = 0
    peak_display_gap_ms: float = 0.0
    # Peak CPU busy is needed alongside peak CPU wait: busy means the CPU was the
    # bottleneck, wait means it was blocked on something else.
    peak_cpu_busy_ms: float = 0.0
    peak_gpu_wait_ms: float = 0.0
    peak_input_latency_ms: float = 0.0
    display_stall_minor_count: int = 0
    display_stall_major_count: int = 0
    peak_display_excess_ms: float = 0.0
    attribution: Optional[FrameAttribution] = None


@dataclass
class CompatibilitySample:
    timestamp: datetime
    target_process: str
    process_id: int
    hwnd: int
    window_title: str
    is_foreground: bool
    is_hung: bool
    response_time_ms: float
    process_cpu_percent: float
    process_memory_mb: float
    process_read_kb_s: float
    process_write_kb_s: float
    thread_count: int
    visual_hash: int = 0
    visual_change_ratio: float = 0.0
    visual_frozen_streak: int = 0


@dataclass
class CompatibilityMetricsSnapshot:
    updated_at: datetime
    target_process: str
    process_id: int
    response_time_ms: float
    process_cpu_percent: float
    process_memory_mb: float
    process_read_kb_s: float
    process_write_kb_s: float
    thread_count: int
    is_hung: bool
    visual_hash: int = 0
    visual_change_ratio: float = 0.0
    visual_frozen_streak: int = 0
