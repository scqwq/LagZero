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
    top_processes: list[ProcessSample] = field(default_factory=list)


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
    duration_seconds: float = 0.0
    snapshot_id: Optional[int] = None


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
    timestamp: datetime
    process_name: str
    process_id: int
    swap_chain: str
    runtime: str
    present_mode: str
    sync_interval: int
    allows_tearing: bool
    frame_time_ms: float
    cpu_busy_ms: float = 0.0
    cpu_wait_ms: float = 0.0
    gpu_busy_ms: float = 0.0
    displayed_time_ms: float = 0.0
    raw_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class FrameMetricsSnapshot:
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
