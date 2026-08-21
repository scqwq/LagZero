"""
core/presentmon_bridge.py — PresentMon process manager and frame parser.

Runs PresentMon as an external child process, consumes CSV rows from stdout,
and emits parsed frame samples plus a lightweight rolling summary for the UI.
"""
from __future__ import annotations

import codecs
import csv
import io
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from time import monotonic

from PySide6.QtCore import QObject, QProcess, QThread, QTimer, Signal, Slot

from core import elevation
from core.models import FrameMetricsSnapshot, FrameSample


DEFAULT_PRESENTMON_DIR = Path(__file__).parent.parent / "tools" / "PresentMon"
DEFAULT_PRESENTMON_PATH = DEFAULT_PRESENTMON_DIR / "PresentMon.exe"
STARTUP_TIMEOUT_MS = 5000
# PresentMon needs a moment after terminate() to call StopTrace and close its
# real-time ETW session; killing it before that orphans the session.
GRACEFUL_EXIT_TIMEOUT_MS = 2000
FORCED_EXIT_TIMEOUT_MS = 1500
METRICS_EMIT_INTERVAL_S = 0.2
LIKELY_UNSUPPORTED_HIGH_PRECISION_TARGETS = {"java.exe", "javaw.exe"}
# v2 exposes CPUBusy/CPUWait/GPUBusy/GPUWait as separate columns, which is what
# makes CPU-vs-GPU attribution possible at all. v1 is still parsed for older
# builds, but we ask for v2.
METRICS_FLAG = "--v2_metrics"


def _median(values: list[float]) -> float:
    """Median without importing statistics on a per-frame hot path."""
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass
class CleanupReport:
    """
    Outcome of one stale-session sweep.

    A bare count cannot express the case that actually matters: orphans were
    found but "logman stop" was denied. The UI needs to tell the user to relaunch
    elevated in that case, not report a successful cleanup of zero sessions.
    """
    found: int = 0
    stopped: int = 0
    needs_elevation: bool = False
    detail: str = ""

    @property
    def failed(self) -> int:
        return max(0, self.found - self.stopped)


class _StreamTextDecoder:
    """
    Stateful byte -> text decoder for PresentMon's stdout/stderr.

    PresentMon writes UTF-16LE with a byte-order mark, even when its streams are
    redirected to pipes. Two consequences drive this class:

      - Decoding must be incremental. A chunk boundary can fall in the middle of
        a UTF-16 code unit (or a surrogate pair), so decoding each chunk
        independently corrupts characters at the seams.
      - The encoding must be sniffed from the BOM rather than assumed. Decoding
        UTF-16 as UTF-8 yields text with an interleaved NUL after every
        character, which breaks CSV parsing and substring matching alike.

    Falls back to UTF-8 for builds that emit no BOM.
    """

    # UTF-8's BOM is three bytes, so buffer that many before deciding.
    _SNIFF_BYTES = 3

    def __init__(self):
        self.reset()

    def reset(self):
        self._decoder = None
        self._pending = b""

    def decode(self, payload: bytes) -> str:
        if not payload:
            return ""
        if self._decoder is None:
            self._pending += payload
            if len(self._pending) < self._SNIFF_BYTES:
                # Not enough bytes to identify the encoding yet. A stream that
                # never grows past this holds no decodable content anyway.
                return ""
            payload, self._pending = self._pending, b""
            encoding = self._sniff_encoding(payload)
            # "replace" keeps the stream alive on genuinely invalid bytes;
            # incomplete trailing sequences are buffered by the decoder itself.
            self._decoder = codecs.getincrementaldecoder(encoding)("replace")
        return self._decoder.decode(payload)

    @staticmethod
    def _sniff_encoding(prefix: bytes) -> str:
        # The "utf-16" codec consumes the BOM and derives endianness from it.
        if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        if prefix.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        return "utf-8"


class _PresentMonParser(QObject):
    sample_parsed = Signal(object)
    metrics_ready = Signal(object)
    header_seen = Signal()

    def __init__(self):
        super().__init__()
        self._headers: list[str] = []
        self._stdout_buffer = ""
        self._recent_frames: deque[FrameSample] = deque(maxlen=180)
        self._last_metrics_emit_ts = 0.0
        self._malformed_line_count = 0

    @Slot()
    def reset_stream(self):
        self._headers = []
        self._stdout_buffer = ""
        self._recent_frames.clear()
        self._last_metrics_emit_ts = 0.0
        self._malformed_line_count = 0

    @Slot(str)
    def process_chunk(self, chunk: str):
        if not chunk:
            return
        self._stdout_buffer += chunk
        while "\n" in self._stdout_buffer:
            raw_line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            line = raw_line.strip()
            if not line:
                continue
            # A single malformed row must never propagate out of this slot: an
            # exception here would abandon the rest of the buffered chunk and
            # leave the stream de-synchronised for every later frame.
            try:
                self._handle_csv_line(line)
            except Exception:
                self._malformed_line_count += 1

    def _handle_csv_line(self, line: str):
        row = next(csv.reader(io.StringIO(line)), None)
        if not row:
            return
        if not self._headers:
            self._headers = row
            self.header_seen.emit()
            return
        if len(row) != len(self._headers):
            return

        data = dict(zip(self._headers, row))
        sample = self._parse_sample(data)
        if sample is None:
            return

        self._recent_frames.append(sample)
        self.sample_parsed.emit(sample)
        now = monotonic()
        if len(self._recent_frames) == 1 or (now - self._last_metrics_emit_ts) >= METRICS_EMIT_INTERVAL_S:
            self._last_metrics_emit_ts = now
            self.metrics_ready.emit(self._build_metrics())

    def _build_metrics(self) -> FrameMetricsSnapshot:
        latest = self._recent_frames[-1]
        frames = list(self._recent_frames)
        frame_times = [s.frame_time_ms for s in frames]
        frame_times_sorted = sorted(frame_times)
        p95_index = min(len(frame_times_sorted) - 1, max(0, int(len(frame_times_sorted) * 0.95) - 1))
        avg_frame_time = sum(frame_times) / len(frame_times)
        # FPS from the average frame time, not from 1/median: a window with one
        # 200 ms hitch should show the FPS the player actually felt.
        fps = 1000.0 / avg_frame_time if avg_frame_time > 0 else 0.0
        return FrameMetricsSnapshot(
            updated_at=datetime.now(),
            target_process=latest.process_name,
            process_id=latest.process_id,
            sample_count=len(frame_times),
            fps=fps,
            avg_frame_time_ms=avg_frame_time,
            p95_frame_time_ms=frame_times_sorted[p95_index],
            max_frame_time_ms=max(frame_times),
            cpu_busy_ms=latest.cpu_busy_ms,
            cpu_wait_ms=latest.cpu_wait_ms,
            gpu_busy_ms=latest.gpu_busy_ms,
            present_mode=latest.present_mode,
            median_frame_time_ms=_median(frame_times),
            median_cpu_busy_ms=_median([s.cpu_busy_ms for s in frames]),
            median_gpu_busy_ms=_median([s.gpu_busy_ms for s in frames]),
            gpu_wait_ms=latest.gpu_wait_ms,
            dropped_frame_count=sum(1 for s in frames if not s.was_displayed),
            input_latency_ms=latest.input_latency_ms,
            metrics_version=latest.metrics_version,
        )

    @staticmethod
    def _parse_sample(data: dict[str, str]) -> FrameSample | None:
        """
        Build a FrameSample from one CSV row, in either metric vocabulary.

        The v1 mapping used to be transposed: "CPU busy" read msInPresentAPI
        (time inside the Present call) and "CPU wait" read msUntilRenderComplete
        (time until the GPU finished). Measured side by side against v2 on the
        same machine, the old cpu_wait ran ~5x too high and cpu_busy ~5x too low,
        which silently inverted every CPU-vs-GPU verdict built on top of them.
        v2 exposes CPUBusy/CPUWait/GPUBusy/GPUWait directly, so it is preferred;
        v1 is still mapped as closely as its columns allow for older builds.
        """
        to_f = _PresentMonParser._to_float
        opt_f = _PresentMonParser._to_optional_float
        is_v2 = "FrameTime" in data or "CPUBusy" in data

        frame_time_ms = to_f(data.get("FrameTime")) if is_v2 else to_f(data.get("msBetweenPresents"))
        if frame_time_ms <= 0:
            # Also covers rows where the column was NA: a frame with no duration
            # carries no pacing information and would divide into the averages.
            return None

        if is_v2:
            displayed_time = opt_f(data.get("DisplayedTime"))
            animation_error = opt_f(data.get("AnimationError"))
            return FrameSample(
                timestamp=datetime.now(),
                process_name=data.get("Application", ""),
                process_id=_PresentMonParser._to_int(data.get("ProcessID")),
                swap_chain=data.get("SwapChainAddress", ""),
                runtime=data.get("PresentRuntime", ""),
                present_mode=data.get("PresentMode", ""),
                sync_interval=_PresentMonParser._to_int(data.get("SyncInterval")),
                allows_tearing=_PresentMonParser._to_bool(data.get("AllowsTearing")),
                frame_time_ms=frame_time_ms,
                cpu_busy_ms=to_f(data.get("CPUBusy")),
                cpu_wait_ms=to_f(data.get("CPUWait")),
                gpu_busy_ms=to_f(data.get("GPUBusy")),
                gpu_wait_ms=to_f(data.get("GPUWait")),
                gpu_time_ms=to_f(data.get("GPUTime")),
                gpu_latency_ms=to_f(data.get("GPULatency")),
                # DisplayedTime is NA exactly when the frame never reached the
                # screen. Reading that as 0.0 would have turned a dropped frame
                # into a frame "displayed for zero milliseconds" and hidden the
                # entire not-displayed stutter class.
                displayed_time_ms=displayed_time if displayed_time is not None else 0.0,
                display_latency_ms=to_f(data.get("DisplayLatency")),
                was_displayed=displayed_time is not None,
                animation_error_ms=animation_error if animation_error is not None else 0.0,
                has_animation_error=animation_error is not None,
                input_latency_ms=to_f(data.get("AllInputToPhotonLatency")),
                click_latency_ms=to_f(data.get("ClickToPhotonLatency")),
                flip_delay_ms=to_f(data.get("MsFlipDelay")),
                capture_time_s=to_f(data.get("CPUStartTime")),
                present_flags=_PresentMonParser._to_int(data.get("PresentFlags")),
                metrics_version="v2",
                raw_fields=data,
            )

        # --- v1 ---
        # v1 has no CPUBusy/CPUWait split. msInPresentAPI is the closest thing to
        # CPU-side work for the present, and msUntilRenderComplete is genuinely a
        # wait, so they map to busy/wait respectively rather than being swapped.
        dropped = _PresentMonParser._to_bool(data.get("Dropped"))
        display_change = to_f(data.get("msBetweenDisplayChange"))
        return FrameSample(
            timestamp=datetime.now(),
            process_name=data.get("Application", ""),
            process_id=_PresentMonParser._to_int(data.get("ProcessID")),
            swap_chain=data.get("SwapChainAddress", ""),
            runtime=data.get("Runtime", ""),
            present_mode=data.get("PresentMode", ""),
            sync_interval=_PresentMonParser._to_int(data.get("SyncInterval")),
            allows_tearing=_PresentMonParser._to_bool(data.get("AllowsTearing")),
            frame_time_ms=frame_time_ms,
            cpu_busy_ms=to_f(data.get("msInPresentAPI")),
            cpu_wait_ms=to_f(data.get("msUntilRenderComplete")),
            gpu_busy_ms=to_f(data.get("msGPUActive")),
            gpu_time_ms=to_f(data.get("msGPUActive")),
            gpu_latency_ms=to_f(data.get("msUntilRenderStart")),
            # v1 states dropped frames outright, and reports the display-change
            # interval instead of a per-frame displayed duration.
            displayed_time_ms=0.0 if dropped else display_change,
            display_latency_ms=to_f(data.get("msUntilDisplayed")),
            was_displayed=not dropped,
            input_latency_ms=to_f(data.get("msSinceInput")),
            flip_delay_ms=to_f(data.get("msFlipDelay")),
            capture_time_s=to_f(data.get("TimeInSeconds")),
            present_flags=_PresentMonParser._to_int(data.get("PresentFlags")),
            metrics_version="v1",
            raw_fields=data,
        )

    @staticmethod
    def _to_optional_float(value: str | None) -> float | None:
        """
        None for PresentMon's "NA", a float otherwise.

        PresentMon writes the literal string NA for metrics it could not compute
        for a given frame. Collapsing that to 0.0 is what would let the report
        claim a measurement that was never taken.
        """
        text = (value or "").strip()
        if not text or text.upper() == "NA":
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _to_float(value: str | None) -> float:
        parsed = _PresentMonParser._to_optional_float(value)
        return parsed if parsed is not None else 0.0

    @staticmethod
    def _to_int(value: str | None) -> int:
        parsed = _PresentMonParser._to_optional_float(value)
        return int(parsed) if parsed is not None else 0

    @staticmethod
    def _to_bool(value: str | None) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes"}


class PresentMonBridge(QObject):
    frame_captured = Signal(object)
    metrics_updated = Signal(object)
    status_changed = Signal(str)
    target_changed = Signal(str)
    capture_identity_changed = Signal(str)
    diagnostics_changed = Signal(str)
    error_occurred = Signal(str)
    _parser_reset_requested = Signal()
    _stdout_chunk_received = Signal(str)
    _probe_pause_requested = Signal(object)
    _probe_resume_requested = Signal(object)
    _recovery_apply_requested = Signal(object)

    def __init__(self, executable: Path | None = None, parent=None):
        super().__init__(parent)
        self._executable = Path(executable) if executable else DEFAULT_PRESENTMON_PATH
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)
        self._process.errorOccurred.connect(self._on_process_error)
        self._process.finished.connect(self._on_process_finished)
        self._process.stateChanged.connect(self._on_process_state_changed)

        # PresentMon emits UTF-16LE with a BOM; decoding is stateful because a
        # pipe chunk can split a code unit. One decoder per stream.
        self._stdout_decoder = _StreamTextDecoder()
        self._stderr_decoder = _StreamTextDecoder()

        self._target_process = ""
        self._target_pid: int | None = None
        self._prefer_process_name_match = False
        self._last_metrics_ts: datetime | None = None
        self._received_frame = False
        self._received_header = False
        self._last_error_text = ""
        self._last_status_text = "Idle"
        self._session_name = self._stable_session_name()
        self._start_count = 0
        self._auto_retry_done = False
        self._pending_restart = False
        self._trace_sessions_cache: list[str] = []
        self._last_capture_identity = "Capture: Requested: No target"
        self._last_diag_text = ""
        self._last_diag_emit_ts = 0.0
        self._initial_cleanup_done = False
        self._last_stderr_line = ""
        self._last_stderr_emit_ts = 0.0
        self._last_failure_reason = ""
        self._last_cleanup_error = ""
        self._last_cleanup_summary = CleanupReport()
        self._startup_timeout_notified = False
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._on_startup_timeout)

        self._parser_thread = QThread(self)
        self._parser = _PresentMonParser()
        self._parser.moveToThread(self._parser_thread)
        self._parser.sample_parsed.connect(self._on_sample_parsed)
        self._parser.metrics_ready.connect(self._on_metrics_ready)
        self._parser.header_seen.connect(self._on_header_seen)
        self._parser_reset_requested.connect(self._parser.reset_stream)
        self._stdout_chunk_received.connect(self._parser.process_chunk)
        self._probe_pause_requested.connect(self._pause_capture_for_probe)
        self._probe_resume_requested.connect(self._resume_capture_after_probe)
        self._recovery_apply_requested.connect(self._dispatch_recovery_action)
        self._parser_thread.start()

    @property
    def executable_path(self) -> Path:
        return self._resolve_executable()

    def is_available(self) -> bool:
        return self._resolve_executable().exists()

    def requested_target(self) -> tuple[str, int | None]:
        return self._target_process, self._target_pid

    def last_failure_reason(self) -> str:
        return self._last_failure_reason

    def set_target(self, process_name: str = "", pid: int | None = None) -> bool:
        target = (process_name or "").strip()
        pid_value = pid if pid and pid > 0 else None
        if target == self._target_process and pid_value == self._target_pid:
            return False
        self._target_process = target
        self._target_pid = pid_value
        self._prefer_process_name_match = False
        self.target_changed.emit(self.target_description())
        self._emit_capture_identity(f"Requested: {self.target_description()}")
        self._emit_diagnostics(force=True)
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self.restart_capture()
        return True

    @staticmethod
    def _stable_session_name() -> str:
        """
        One ETW session name per LagLense process, reused for every restart.

        Keeping it stable is what lets "--stop_existing_session" reclaim the
        previous session instead of leaving it running as an orphan. The PID
        keeps concurrent LagLense instances from fighting over one name.
        """
        return f"LagLense-{os.getpid()}"

    def target_description(self) -> str:
        if self._target_pid:
            suffix = " via name match" if self._prefer_process_name_match and self._target_process else ""
            return f"PID {self._target_pid} ({self._target_process or 'unknown'}){suffix}"
        if self._target_process:
            return self._target_process
        return "No target"

    def _is_likely_unsupported_high_precision_target(self) -> bool:
        return (self._target_process or "").strip().lower() in LIKELY_UNSUPPORTED_HIGH_PRECISION_TARGETS

    def start_capture(self, force_restart: bool = False):
        if not self.is_available():
            self.status_changed.emit("PresentMon not found")
            self.error_occurred.emit(
                f"PresentMon executable was not found near {self._executable.parent}"
            )
            return
        if not self._target_process and not self._target_pid:
            self.status_changed.emit("Waiting for target")
            return
        if self._process.state() != QProcess.ProcessState.NotRunning:
            if force_restart:
                self._pending_restart = True
                self.stop_capture()
            return
        if not self._initial_cleanup_done:
            self.cleanup_stale_sessions(include_active=False)
            self._initial_cleanup_done = True

        self._start_count += 1
        # The session name must stay STABLE across restarts within this process.
        # "--stop_existing_session" only matches a session of the same name, so a
        # per-attempt suffix meant every restart orphaned the previous real-time
        # ETW session. Those orphans then competed for the DxgKrnl trace buffers
        # ("events were lost") and eventually exhausted the session limit (1450).
        self._session_name = self._stable_session_name()
        args = [
            "--output_stdout",
            "--no_console_stats",
            METRICS_FLAG,
            "--stop_existing_session",
            "--terminate_on_proc_exit",
            "--session_name",
            self._session_name,
        ]
        if self._target_pid and not self._prefer_process_name_match:
            args.extend(["--process_id", str(self._target_pid)])
        elif self._target_process:
            args.extend(["--process_name", self._target_process])

        self._received_frame = False
        self._received_header = False
        self._last_error_text = ""
        self._last_failure_reason = ""
        self._auto_retry_done = False
        self._pending_restart = False
        self._startup_timeout_notified = False
        # Each PresentMon process writes a fresh BOM, so per-stream decode state
        # must be dropped before the next one starts.
        self._stdout_decoder.reset()
        self._stderr_decoder.reset()
        self._parser_reset_requested.emit()
        self._process.start(str(self._resolve_executable()), args)
        self._startup_timer.start(STARTUP_TIMEOUT_MS)
        self._set_status(f"Starting capture for {self.target_description()}")

    def connect_frame_consumer(self, slot) -> None:
        self._parser.sample_parsed.connect(slot)

    def stop_capture(self):
        self._startup_timer.stop()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return
        self._set_status("Stopping capture…")
        self._terminate_process_gracefully()

    def restart_capture(self):
        self._pending_restart = True
        self.stop_capture()

    def _terminate_process_gracefully(self) -> None:
        """
        Ask PresentMon to exit before forcing it.

        QProcess.kill() maps to TerminateProcess on Windows, which gives
        PresentMon no chance to run its shutdown path and call StopTrace. The
        real-time ETW session then survives the process as an orphan. terminate()
        posts WM_CLOSE / CTRL_BREAK first so the session is closed properly, and
        kill() remains as the fallback for a wedged process.
        """
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return
        self._process.terminate()
        if self._process.waitForFinished(GRACEFUL_EXIT_TIMEOUT_MS):
            return
        # Still alive: force it, then reclaim the session name out-of-band since
        # the orphaned session would otherwise linger until reboot.
        self._process.kill()
        self._process.waitForFinished(FORCED_EXIT_TIMEOUT_MS)
        self._cleanup_named_session(self._session_name)

    def shutdown(self):
        self._startup_timer.stop()
        self._terminate_process_gracefully()
        # Last line of defence: on exit, make sure this process leaves no
        # real-time session behind even if PresentMon died without cleaning up.
        self._cleanup_named_session(self._stable_session_name())
        self._parser_thread.quit()
        self._parser_thread.wait(2000)

    def cleanup_stale_sessions(self, include_active: bool = False) -> int:
        self._last_cleanup_error = ""
        stale = self._find_stale_sessions(force_refresh=True, include_active=include_active)
        stopped = [name for name in stale if self._cleanup_named_session(name)]
        self._refresh_trace_sessions(force=True)
        failed = len(stale) - len(stopped)
        # Recorded separately from the count so callers can tell "found nothing"
        # apart from "found orphans but was not allowed to stop them" — the two
        # were indistinguishable while this returned len(stale) unconditionally.
        self._last_cleanup_summary = CleanupReport(
            found=len(stale),
            stopped=len(stopped),
            needs_elevation="denied" in (self._last_cleanup_error or "").lower()
            or "拒绝访问" in (self._last_cleanup_error or ""),
            detail=self._last_cleanup_error,
        )
        if not stale:
            self._last_status_text = "No stale trace sessions found"
        elif failed:
            self._last_status_text = (
                f"Cleaned {len(stopped)}/{len(stale)} stale trace session(s); "
                f"{failed} failed ({self._last_cleanup_error or 'unknown error'})"
            )
        else:
            self._last_status_text = f"Cleaned {len(stopped)} stale trace session(s)"
        self._emit_diagnostics(force=True)
        return len(stopped)

    def last_cleanup_report(self) -> CleanupReport:
        """The outcome of the most recent cleanup, for UI reporting."""
        return self._last_cleanup_summary

    def probe_active_presents(self, duration_seconds: int = 3) -> str:
        """
        Runs a short unfiltered PresentMon capture and returns a compact summary
        of which processes actually emitted present events. This is intended for
        troubleshooting targets that show NO DATA despite running.
        """
        was_running = self._process.state() != QProcess.ProcessState.NotRunning
        if was_running:
            pause_done = threading.Event()
            self._probe_pause_requested.emit(pause_done)
            pause_done.wait(3.0)
        self.cleanup_stale_sessions(include_active=False)
        session_name = f"LagLenseProbe-{os.getpid()}"
        with tempfile.NamedTemporaryFile(prefix="laglense_probe_", suffix=".csv", delete=False) as tmp:
            temp_path = Path(tmp.name)

        try:
            cmd = [
                str(self._resolve_executable()),
                "--output_file", str(temp_path),
                METRICS_FLAG,
                "--no_console_stats",
                "--stop_existing_session",
                "--session_name", session_name,
                "--timed", str(duration_seconds),
                "--terminate_after_timed",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(10, duration_seconds + 8),
                check=False,
            )

            stderr = (result.stderr or "").strip()
            if result.returncode != 0 and not temp_path.exists():
                return (
                    f"Probe failed (exit {result.returncode}). "
                    f"stderr={stderr or 'none'}"
                )

            if not temp_path.exists():
                return f"Probe produced no CSV file. stderr={stderr or 'none'}"

            # Sniff the BOM instead of assuming UTF-8. PresentMon writes UTF-16LE
            # to its stdout/stderr pipes, and the encoding of --output_file could
            # not be confirmed here (an unelevated probe never captures enough to
            # produce a file), so decode the same way the live streams are
            # decoded rather than betting on one of the two.
            decoded = _StreamTextDecoder().decode(temp_path.read_bytes())
            reader = csv.DictReader(io.StringIO(decoded, newline=""))
            rows = list(reader)

            if not rows:
                return f"Probe found no present events in {duration_seconds}s. stderr={stderr or 'none'}"

            summary: dict[tuple[str, str], int] = {}
            for row in rows:
                app = (row.get("Application") or "unknown").strip()
                pid = (row.get("ProcessID") or "0").strip()
                key = (app, pid)
                summary[key] = summary.get(key, 0) + 1

            ranked = sorted(summary.items(), key=lambda item: item[1], reverse=True)[:8]
            lines = [f"Probe saw {len(rows)} present samples in {duration_seconds}s:"]
            for (app, pid), count in ranked:
                lines.append(f"{app} (PID {pid}) -> {count} samples")
            if stderr:
                lines.append(f"stderr={stderr}")
            return "\n".join(lines)
        finally:
            self._cleanup_named_session(session_name)
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            if was_running and (self._target_process or self._target_pid):
                resume_done = threading.Event()
                self._probe_resume_requested.emit(resume_done)
                resume_done.wait(3.0)

    def recover_high_precision_target(self, duration_seconds: int = 3) -> str:
        target_name = (self._target_process or "").strip()
        if not target_name and not self._target_pid:
            return "Manual recovery requires a target process first."
        lowered_failure = (self._last_failure_reason or "").lower()
        target_desc = self.target_description()
        using_name_match = bool(self._target_process)
        clean_count = 0

        # When ETW session startup is already failing, avoid creating an extra
        # probe session. Clean stale LagLense/PresentMon sessions and retry the
        # main capture directly to keep session churn low.
        if "1450" in lowered_failure:
            clean_count = self.cleanup_stale_sessions(include_active=False)

        apply_result = self._restart_capture_sync(prefer_process_name=using_name_match)
        if not apply_result.get("applied"):
            if "1450" in lowered_failure:
                return (
                    f"Recovery could not restart high-precision capture for {target_desc} after "
                    f"cleaning {clean_count} stale session(s). PresentMon is still blocked by trace session error 1450."
                )
            return f"Recovery could not restart high-precision capture for {target_desc}."

        if "1450" in lowered_failure:
            mode_hint = "process-name match" if using_name_match else "current PID"
            return (
                f"Recovery cleaned {clean_count} stale session(s) and restarted high-precision capture "
                f"for {target_desc} using {mode_hint}. Waiting for frame data."
            )

        if using_name_match and self._target_pid:
            return (
                f"Recovery restarted high-precision capture for {target_desc} using process-name match "
                "to reduce PID churn. Waiting for frame data."
            )
        return f"Recovery restarted high-precision capture for {target_desc}. Waiting for frame data."

    def _read_stdout(self):
        chunk = self._stdout_decoder.decode(bytes(self._process.readAllStandardOutput()))
        if not chunk:
            return
        self._stdout_chunk_received.emit(chunk)

    def _read_stderr(self):
        text = self._stderr_decoder.decode(bytes(self._process.readAllStandardError())).strip()
        if text:
            line = text.splitlines()[-1][:240]
            lowered = line.lower()
            now = monotonic()
            if line == self._last_stderr_line and (now - self._last_stderr_emit_ts) < 2.0:
                return
            self._last_stderr_line = line
            self._last_stderr_emit_ts = now
            self._last_error_text = line
            if "access denied" in lowered:
                self._last_failure_reason = "access denied"
                self.error_occurred.emit(
                    "PresentMon failed with ACCESS DENIED. Please restart LagLense as Administrator."
                )
            if "1450" in lowered:
                self._last_failure_reason = "trace session error 1450"
                self.error_occurred.emit(
                    "PresentMon failed with trace session error 1450. Try 'Clean Stale Sessions' first; if it persists, reboot Windows."
                )
                self._set_status("PresentMon trace session error 1450. Waiting for manual cleanup.")
                return
            if "events were lost" in lowered or "30034" in lowered or "18717" in lowered:
                self._last_failure_reason = "events were lost"
                self._set_status("PresentMon warning: ETW events were lost; capture may be incomplete.")
                return
            if "session" in lowered and "different name" in lowered:
                self._last_failure_reason = "session name conflict"
                self.error_occurred.emit(
                    f"PresentMon session conflict on {self._session_name}. Will need a fresh restart."
                )
            self._set_status(line[:160])

    @Slot()
    def _on_header_seen(self):
        self._received_header = True
        self._startup_timeout_notified = False
        self._last_failure_reason = ""
        self._startup_timer.stop()
        self._set_status(f"Capturing {self.target_description()}")

    @Slot(object)
    def _on_sample_parsed(self, sample: FrameSample):
        self._received_frame = True
        self._last_failure_reason = ""

    @Slot(object)
    def _on_metrics_ready(self, metrics: FrameMetricsSnapshot):
        self._last_metrics_ts = metrics.updated_at
        self._emit_capture_identity(
            f"Captured: {metrics.target_process or 'unknown'} (PID {metrics.process_id})"
        )
        self._emit_diagnostics()
        self.metrics_updated.emit(metrics)

    def _on_process_error(self, error):
        self._startup_timer.stop()
        self._last_error_text = f"Process error: {error}"
        self._last_failure_reason = f"process error: {error}"
        self._emit_diagnostics(force=True)
        self.error_occurred.emit(f"PresentMon process error: {error}")

    def _on_process_finished(self, exit_code: int, _status):
        self._startup_timer.stop()
        if self._pending_restart:
            self._set_status(f"Restarting capture for {self.target_description()}…")
        elif not self._received_frame and exit_code != 0:
            self._set_status(f"Capture failed ({exit_code})")
        else:
            self._set_status(f"Capture exited ({exit_code})")
        if self._pending_restart:
            self._pending_restart = False
            self.start_capture()

    @Slot(QProcess.ProcessState)
    def _on_process_state_changed(self, state):
        if state == QProcess.ProcessState.NotRunning:
            self._startup_timer.stop()
        self._emit_diagnostics(force=True)

    @Slot()
    def _on_startup_timeout(self):
        if self._process.state() != QProcess.ProcessState.Running:
            return
        if self._received_frame or self._received_header:
            return
        if self._startup_timeout_notified:
            return
        self._startup_timeout_notified = True
        if self._is_likely_unsupported_high_precision_target():
            self.error_occurred.emit(
                f"No frame data for {self.target_description()}. "
                "Minecraft 1.8.9 and other legacy Java/OpenGL titles often do not expose PresentMon high-precision frame events."
            )
            return
        if self._retry_with_process_name():
            return
        self._set_status(
            f"No frame data for {self.target_description()} yet. "
            "Use the exact .exe name, pick a detected foreground window, or run 'Probe Present' to find the real presenting process."
        )

    def _retry_with_process_name(self) -> bool:
        if self._prefer_process_name_match or not self._target_pid or not self._target_process:
            return False
        self._prefer_process_name_match = True
        self._startup_timeout_notified = False
        self._set_status(
            f"No frame data for PID {self._target_pid}. Retrying PresentMon with process name {self._target_process}."
        )
        self.restart_capture()
        return True

    @staticmethod
    def _parse_probe_matches(result: str) -> list[tuple[str, int, int]]:
        matches: list[tuple[str, int, int]] = []
        for line in (result or "").splitlines():
            match = re.search(r"^(?P<app>.+?) \(PID (?P<pid>\d+)\) -> (?P<count>\d+) samples$", line.strip())
            if not match:
                continue
            matches.append(
                (
                    match.group("app").strip(),
                    int(match.group("pid")),
                    int(match.group("count")),
                )
            )
        return matches

    def find_recovery_candidate(
        self,
        result: str,
        process_name: str | None = None,
        pid: int | None = None,
    ) -> tuple[str, int, int] | None:
        matches = self._parse_probe_matches(result)
        if not matches:
            return None

        target_name = (process_name if process_name is not None else self._target_process or "").strip()
        target_pid = pid if pid and pid > 0 else self._target_pid
        lowered = target_name.lower()
        target_stem = lowered.removesuffix(".exe")

        ranked: list[tuple[int, int, str, int]] = []
        for app, match_pid, count in matches:
            app_lower = app.lower()
            app_stem = app_lower.removesuffix(".exe")
            pid_match = bool(target_pid and match_pid == target_pid)
            exact_match = bool(lowered and app_lower == lowered)
            stem_match = bool(target_stem and app_stem == target_stem)
            if not (pid_match or exact_match or stem_match):
                continue
            score = count
            if stem_match:
                score += 10_000
            if exact_match:
                score += 20_000
            if pid_match:
                score += 40_000
            ranked.append((score, count, app, match_pid))

        if not ranked:
            return None

        ranked.sort(reverse=True)
        _, count, app, match_pid = ranked[0]
        return app, match_pid, count

    def _apply_recovery_target_sync(self, process_name: str, pid: int) -> dict[str, bool]:
        payload = {
            "process_name": process_name,
            "pid": pid,
            "applied": False,
            "changed": False,
            "started": False,
        }
        if QThread.currentThread() == self.thread():
            self._apply_recovery_target(payload)
            return payload
        done_event = threading.Event()
        payload["done_event"] = done_event
        self._recovery_apply_requested.emit(payload)
        done_event.wait(3.0)
        return payload

    @Slot(object)
    def _dispatch_recovery_action(self, payload):
        if "process_name" in payload or "pid" in payload:
            self._apply_recovery_target(payload)
            return
        self._restart_capture_with_mode(payload)

    @Slot(object)
    def _apply_recovery_target(self, payload):
        process_name = str(payload.get("process_name") or "").strip()
        pid = self._to_int(str(payload.get("pid") or 0))
        changed = False
        started = False
        if process_name and pid > 0:
            changed = self.set_target(process_name, pid=pid)
            if not changed and self._prefer_process_name_match:
                self._prefer_process_name_match = False
                self.target_changed.emit(self.target_description())
                self._emit_capture_identity(f"Requested: {self.target_description()}")
                changed = True

            if changed:
                self.start_capture(force_restart=True)
                started = True
            elif self._process.state() == QProcess.ProcessState.NotRunning:
                self.start_capture()
                started = True

        payload["changed"] = changed
        payload["started"] = started
        payload["applied"] = bool(process_name and pid > 0 and (changed or started or self._target_pid == pid))
        done_event = payload.get("done_event")
        if hasattr(done_event, "set"):
            done_event.set()

    def _restart_capture_sync(self, prefer_process_name: bool = False) -> dict[str, bool]:
        payload = {
            "prefer_process_name": prefer_process_name,
            "applied": False,
            "restarted": False,
        }
        if QThread.currentThread() == self.thread():
            self._restart_capture_with_mode(payload)
            return payload
        done_event = threading.Event()
        payload["done_event"] = done_event
        self._recovery_apply_requested.emit(payload)
        done_event.wait(3.0)
        return payload

    @Slot(object)
    def _restart_capture_with_mode(self, payload):
        prefer_process_name = bool(payload.get("prefer_process_name"))
        restarted = False
        had_target = bool(self._target_process or self._target_pid)
        if had_target:
            if prefer_process_name and self._target_process:
                if not self._prefer_process_name_match:
                    self._prefer_process_name_match = True
                    self.target_changed.emit(self.target_description())
                    self._emit_capture_identity(f"Requested: {self.target_description()}")
                self.start_capture(force_restart=True)
                restarted = True
            elif self._process.state() == QProcess.ProcessState.NotRunning:
                self.start_capture()
                restarted = True
            else:
                self.start_capture(force_restart=True)
                restarted = True
        payload["restarted"] = restarted
        payload["applied"] = had_target and restarted
        done_event = payload.get("done_event")
        if hasattr(done_event, "set"):
            done_event.set()

    def _resolve_executable(self) -> Path:
        if self._executable.exists():
            return self._executable
        if self._executable.parent.exists():
            matches = sorted(self._executable.parent.glob("PresentMon*.exe"))
            if matches:
                return matches[0]
        return self._executable

    @staticmethod
    def _to_float(value: str | None) -> float:
        try:
            return float(value or 0.0)
        except ValueError:
            return 0.0

    @staticmethod
    def _to_int(value: str | None) -> int:
        try:
            return int(float(value or 0))
        except ValueError:
            return 0

    @staticmethod
    def _to_bool(value: str | None) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    def _set_status(self, message: str):
        self._last_status_text = message
        self.status_changed.emit(message)
        self._emit_diagnostics(force=True)

    @Slot(object)
    def _pause_capture_for_probe(self, done_event):
        self._startup_timer.stop()
        # Graceful stop matters most here: the probe is about to start its own
        # session, so leaving this one orphaned would double the buffer pressure
        # that causes "events were lost" in the very measurement being taken.
        self._terminate_process_gracefully()
        if hasattr(done_event, "set"):
            done_event.set()

    @Slot(object)
    def _resume_capture_after_probe(self, done_event):
        if self._target_process or self._target_pid:
            self.start_capture()
        if hasattr(done_event, "set"):
            done_event.set()

    def _emit_capture_identity(self, description: str):
        if description == self._last_capture_identity:
            return
        self._last_capture_identity = description
        self.capture_identity_changed.emit(description)

    def _emit_diagnostics(self, force: bool = False):
        state_map = {
            QProcess.ProcessState.NotRunning: "NotRunning",
            QProcess.ProcessState.Starting: "Starting",
            QProcess.ProcessState.Running: "Running",
        }
        state = state_map.get(self._process.state(), "Unknown")
        sessions = list(self._trace_sessions_cache)
        trace_total = len(sessions)
        top_sessions = self._top_trace_sessions(sessions, limit=6)
        top_summary = ", ".join(top_sessions) if top_sessions else "none"
        diag = (
            f"exe={self._resolve_executable().name} | session={self._session_name} | state={state}\n"
            f"elevated={elevation.is_elevated()} | trace_total={trace_total} | likely_remaining_budget={'low' if trace_total >= 40 else 'medium' if trace_total >= 25 else 'healthy'} | top={top_summary}\n"
            f"target={self.target_description()} | header_seen={self._received_header} | received_frame={self._received_frame}\n"
            f"status={self._last_status_text}\n"
            f"cleanup_error={self._last_cleanup_error or 'none'}\n"
            f"stderr={self._last_error_text or 'none'}"
        )
        now = monotonic()
        if not force and diag == self._last_diag_text and (now - self._last_diag_emit_ts) < 1.0:
            return
        if not force and (now - self._last_diag_emit_ts) < 1.0:
            return
        self._last_diag_text = diag
        self._last_diag_emit_ts = now
        self.diagnostics_changed.emit(diag)

    def _find_stale_sessions(self, force_refresh: bool = False, include_active: bool = False) -> list[str]:
        names = self._refresh_trace_sessions(force=force_refresh)
        current = self._session_name.lower()
        stale = []
        for name in names:
            # "LagLense" without the dash also catches "LagLenseProbe-<pid>": the
            # probe cleans up after itself, but if that cleanup was denied the
            # orphan would otherwise never be reclaimed by anything.
            if not (name.startswith("LagLense") or name == "PresentMon"):
                continue
            if not include_active and name.lower() == current:
                continue
            stale.append(name)
        return stale

    def _cleanup_named_session(self, session_name: str) -> bool:
        """
        Stop one real-time session, recording why it failed.

        The previous version swallowed every error, so an unelevated 'logman stop'
        (which returns access denied) looked indistinguishable from a successful
        cleanup. Callers now get a boolean and the reason lands in diagnostics.
        """
        stopped, detail = elevation.stop_trace_session(session_name)
        if not stopped:
            self._last_cleanup_error = detail
        return stopped

    def _list_trace_sessions(self) -> list[str]:
        """
        Enumerate running real-time ETW sessions, independent of console locale.

        The previous implementation matched the literal English strings "Trace",
        "Running" and "Data Collector Set". On a Chinese Windows install logman
        prints 跟踪 / 正在运行 / 数据收集器集, so every line was rejected and the
        session list came back empty — which made stale-session cleanup a no-op
        no matter how many orphans existed.

        Only two things about the layout are actually locale-stable: a dashed
        separator follows the header, and every session row is
        "<name>  <type>  <status>" separated by runs of 2+ spaces. Since
        "logman query -ets" lists nothing but trace sessions, the type column
        does not need to be inspected at all.
        """
        try:
            result = subprocess.run(
                ["logman", "query", "-ets"],
                capture_output=True,
                timeout=8,
                check=False,
                # Bytes, not text=True: the default locale decode mangles the
                # non-ASCII column labels and can raise on some code pages.
            )
        except Exception:
            return []

        stdout = elevation.decode_console(result.stdout)
        names: list[str] = []
        past_header = False
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if set(stripped) <= {"-"}:
                # The dashed rule under the header; rows start after it.
                past_header = True
                continue
            if not past_header:
                continue
            columns = re.split(r"\s{2,}", stripped)
            # Trailing chatter ("The command completed successfully.") is a
            # single column because it has no run of 2+ spaces.
            if len(columns) < 3:
                continue
            name = columns[0].strip()
            if name:
                names.append(name)
        return names

    def _refresh_trace_sessions(self, force: bool = False) -> list[str]:
        if not force:
            return self._trace_sessions_cache
        self._trace_sessions_cache = self._list_trace_sessions()
        return self._trace_sessions_cache

    def _top_trace_sessions(self, names: list[str], limit: int = 6) -> list[str]:
        priority = []
        for name in names:
            lowered = name.lower()
            score = 0
            if lowered.startswith("laglense-"):
                score += 100
            if "presentmon" in lowered:
                score += 90
            if "steam" in lowered:
                score += 70
            if "nvidia" in lowered:
                score += 60
            if "gaming" in lowered:
                score += 50
            score += min(len(name), 30)
            priority.append((score, name))
        priority.sort(reverse=True)
        return [name for _, name in priority[:limit]]
