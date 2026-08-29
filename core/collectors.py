"""
core/collectors.py — Background QThread collectors for CPU, RAM, Processes, Responsiveness.

Each collector runs in its own thread and emits a signal every second.
The UI and detection engine connect to these signals — fully decoupled.
"""
import os
import time
import statistics
from datetime import datetime

import psutil
from PySide6.QtCore import QThread, Signal

from core.gpu_stats import query_gpu_memory
from core.models import SystemSample, ProcessSample, ProcessGroupSample, TargetProcessMetrics


PROCESS_REFRESH_INTERVAL = 3
GPU_REFRESH_INTERVAL = 2
BROWSER_PROCESS_GROUPS = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "opera.exe": "Opera",
}

# "System Idle Process" (PID 0) accounts for the time the CPU spent doing
# NOTHING. On an idle machine psutil reports it near 100%, and because the top
# list is sorted by CPU it landed first, so the cause analyzer's "one process is
# hogging the CPU" rule blamed the idle counter for the stutter. Its sibling
# pseudo-processes carry kernel/interrupt time that no user can act on either, so
# they are excluded from the "who is eating the CPU" ranking as well.
IDLE_PSEUDO_PIDS = frozenset({0})
IDLE_PSEUDO_NAMES = frozenset({
    "system idle process",
    "idle",
})

# psutil's per-process cpu_percent is normalised to ONE core: 100% means one
# full core, so a 32-thread machine shows a modern game at 300–2000%. Rules and
# detectors that compare against a whole-machine threshold were firing on that
# raw number (a game at 200% on 32 threads is 6% of the machine and perfectly
# healthy), so every threshold now consumes the machine-share ratio instead.
_cpu_count_cache: int | None = None


def machine_cpu_count() -> int:
    """Logical processor count, cached (os.cpu_count() walks syscalls each call)."""
    global _cpu_count_cache
    if _cpu_count_cache is None:
        _cpu_count_cache = os.cpu_count() or 1
    return _cpu_count_cache


def per_core_to_machine_share(cpu_percent: float) -> float:
    """
    Convert a psutil per-process CPU % (100% = one core) into a share of the
    whole machine (0–100). psutil already expresses the value in percent, so a
    200% reading on a 32-thread machine is 200/32 = 6.25 — no extra ×100. The
    denominator floor keeps a bogus core count from dividing by zero.
    """
    return cpu_percent / machine_cpu_count()


# ---------------------------------------------------------------------------
# Responsiveness probe
# ---------------------------------------------------------------------------

def is_idle_pseudo_process(pid: int | None, name: str | None) -> bool:
    """
    True for the kernel's idle bookkeeping entries.

    Matched on both PID and name because the PID is stable but the name is
    localised on non-English Windows, and a name-only check would miss it.
    """
    if pid in IDLE_PSEUDO_PIDS:
        return True
    return (name or "").strip().lower() in IDLE_PSEUDO_NAMES


def measure_responsiveness_ms() -> float:
    """
    Measure how long a trivial OS operation takes (in milliseconds).

    A healthy system completes this in < 5 ms.
    A stressed system (paging, scheduler contention) may take 50–500 ms.

    We use time.sleep(0.001) accuracy as a proxy: we ask for 1 ms sleep and
    measure how long it actually takes.  Repeated 5 times; we return the median.
    """
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        time.sleep(0.001)   # 1 ms
        elapsed_ms = (time.perf_counter() - t0) * 1000
        samples.append(elapsed_ms)
    return statistics.median(samples)


# ---------------------------------------------------------------------------
# Main collector thread
# ---------------------------------------------------------------------------

class SystemCollector(QThread):
    """
    Collects all system metrics every `interval` seconds.
    Emits `sample_ready` with a filled SystemSample dataclass.

    Why one thread instead of four separate ones?
    → Fewer synchronisation headaches; all metrics share the same timestamp.
    """

    sample_ready = Signal(object)   # emits SystemSample
    error_occurred = Signal(str)

    def __init__(
        self,
        interval: float = 1.0,
        top_n_processes: int = 10,
        process_selector=None,
        parent=None,
    ):
        super().__init__(parent)
        self.interval = interval
        self.top_n = top_n_processes
        self._process_selector = process_selector
        self._running = False
        self._collect_count = 0
        self._last_top_processes: list[ProcessSample] = []
        self._last_process_groups: list = []
        self._tracked_process_name = ""
        self._tracked_process_pid: int | None = None
        self._tracked_process: psutil.Process | None = None
        self._last_tracked_io: tuple[int, int, float] | None = None
        self._last_gpu_memory = None

    # ------------------------------------------------------------------
    def run(self):
        self._running = True
        self._prime_counters()
        while self._running:
            loop_start = time.perf_counter()
            try:
                sample = self._collect()
                self.sample_ready.emit(sample)
            except Exception as exc:  # noqa: BLE001
                self.error_occurred.emit(str(exc))

            # Sleep for the remainder of the interval
            elapsed = time.perf_counter() - loop_start
            remaining = self.interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def stop(self):
        self._running = False
        self.wait(2000)

    def set_tracked_process(self, process_name: str = "", pid: int | None = None):
        self._tracked_process_name = (process_name or "").strip()
        self._tracked_process_pid = pid if pid and pid > 0 else None
        self._tracked_process = None
        self._last_tracked_io = None

    def _prime_counters(self):
        # Prime psutil CPU counters in the worker thread so startup UI is not blocked.
        psutil.cpu_percent(interval=None)
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    # ------------------------------------------------------------------
    def _collect(self) -> SystemSample:
        self._collect_count += 1
        now = datetime.now()

        # --- CPU ---
        cpu_overall = psutil.cpu_percent(interval=None)
        cpu_cores = psutil.cpu_percent(interval=None, percpu=True)

        # --- Memory ---
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # --- Responsiveness probe ---
        responsiveness = measure_responsiveness_ms()

        # --- Top processes ---
        if (
            not self._last_top_processes
            or (self._collect_count % PROCESS_REFRESH_INTERVAL) == 1
        ):
            self._last_top_processes = self._top_processes()
        processes = self._last_top_processes
        process_groups = self._last_process_groups
        target_process = self._collect_tracked_process()
        if self._collect_count % GPU_REFRESH_INTERVAL == 1 or self._last_gpu_memory is None:
            self._last_gpu_memory = query_gpu_memory()

        return SystemSample(
            timestamp=now,
            cpu_percent=cpu_overall,
            cpu_per_core=cpu_cores,
            ram_percent=vm.percent,
            ram_used_mb=vm.used / (1024 ** 2),
            ram_total_mb=vm.total / (1024 ** 2),
            ram_available_mb=vm.available / (1024 ** 2),
            swap_percent=swap.percent,
            responsiveness_ms=responsiveness,
            top_processes=processes,
            process_groups=process_groups,
            target_process=target_process,
            gpu_memory=self._last_gpu_memory,
        )

    def _top_processes(self) -> list[ProcessSample]:
        procs = []
        groups: dict[str, dict[str, float]] = {}
        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_info", "status"]
        ):
            try:
                info = proc.info
                if is_idle_pseudo_process(info["pid"], info["name"]):
                    continue
                mem_mb = (info["memory_info"].rss / (1024 ** 2)) if info["memory_info"] else 0.0
                private_mb = (
                    getattr(info["memory_info"], "private", None) / (1024 ** 2)
                    if info["memory_info"] and getattr(info["memory_info"], "private", None) is not None
                    else mem_mb
                )
                procs.append(ProcessSample(
                    pid=info["pid"],
                    name=info["name"] or "unknown",
                    cpu_percent=info["cpu_percent"] or 0.0,
                    memory_mb=mem_mb,
                    status=info["status"] or "unknown",
                    cpu_machine_share=per_core_to_machine_share(info["cpu_percent"] or 0.0),
                ))
                group_name = BROWSER_PROCESS_GROUPS.get((info["name"] or "").lower())
                if group_name is not None:
                    group = groups.setdefault(group_name, {"cpu": 0.0, "memory": 0.0, "count": 0})
                    group["cpu"] += info["cpu_percent"] or 0.0
                    group["memory"] += private_mb
                    group["count"] += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by CPU first, then RAM as tiebreaker
        procs.sort(key=lambda p: (p.cpu_percent, p.memory_mb), reverse=True)
        self._last_process_groups = [
            ProcessGroupSample(
                name=name,
                process_count=int(values["count"]),
                cpu_machine_share=per_core_to_machine_share(values["cpu"]),
                memory_mb=values["memory"],
            )
            for name, values in groups.items()
        ]
        self._last_process_groups.sort(
            key=lambda group: (group.cpu_machine_share, group.memory_mb),
            reverse=True,
        )
        if self._process_selector is not None:
            return self._process_selector(
                procs,
                self._tracked_process_pid,
                machine_cpu_count(),
            )
        return procs[: self.top_n]

    def _collect_tracked_process(self) -> TargetProcessMetrics | None:
        process = self._resolve_tracked_process()
        if process is None:
            return None
        try:
            mem_info = process.memory_info()
            io_counters = process.io_counters()
            cpu_percent = process.cpu_percent(interval=None)
            thread_count = process.num_threads()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            self._tracked_process = None
            return None

        now = time.perf_counter()
        read_kb_s = 0.0
        write_kb_s = 0.0
        current_io = (io_counters.read_bytes, io_counters.write_bytes, now)
        if self._last_tracked_io is not None:
            prev_read, prev_write, prev_ts = self._last_tracked_io
            elapsed = max(now - prev_ts, 0.001)
            read_kb_s = max(0.0, (current_io[0] - prev_read) / 1024.0 / elapsed)
            write_kb_s = max(0.0, (current_io[1] - prev_write) / 1024.0 / elapsed)
        self._last_tracked_io = current_io
        return TargetProcessMetrics(
            pid=process.pid,
            name=process.name(),
            cpu_percent=cpu_percent,
            memory_mb=mem_info.rss / (1024 ** 2),
            private_memory_mb=getattr(mem_info, "private", 0.0) / (1024 ** 2) if getattr(mem_info, "private", None) is not None else mem_info.rss / (1024 ** 2),
            read_kb_s=read_kb_s,
            write_kb_s=write_kb_s,
            thread_count=thread_count,
            cpu_machine_share=per_core_to_machine_share(cpu_percent),
        )

    def _resolve_tracked_process(self) -> psutil.Process | None:
        if self._tracked_process is not None:
            try:
                if self._tracked_process.is_running():
                    return self._tracked_process
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._tracked_process = None
        if self._tracked_process_pid:
            try:
                self._tracked_process = psutil.Process(self._tracked_process_pid)
                self._tracked_process.cpu_percent(interval=None)
                return self._tracked_process
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._tracked_process = None
                return None
        if not self._tracked_process_name:
            return None
        lowered = self._tracked_process_name.lower()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() == lowered:
                    self._tracked_process_pid = proc.info["pid"]
                    self._tracked_process = psutil.Process(proc.info["pid"])
                    self._tracked_process.cpu_percent(interval=None)
                    return self._tracked_process
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None
