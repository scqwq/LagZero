"""
core/presentmon_bridge.py — PresentMon process manager and frame parser.

Runs PresentMon as an external child process, consumes CSV rows from stdout,
and emits parsed frame samples plus a lightweight rolling summary for the UI.
"""
from __future__ import annotations

import csv
import io
from collections import deque
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from time import monotonic

from PySide6.QtCore import QObject, QProcess, QThread, QTimer, Signal, Slot

from core.models import FrameMetricsSnapshot, FrameSample


DEFAULT_PRESENTMON_DIR = Path(__file__).parent.parent / "tools" / "PresentMon"
DEFAULT_PRESENTMON_PATH = DEFAULT_PRESENTMON_DIR / "PresentMon.exe"
STARTUP_TIMEOUT_MS = 5000
METRICS_EMIT_INTERVAL_S = 0.2
LIKELY_UNSUPPORTED_HIGH_PRECISION_TARGETS = {"java.exe", "javaw.exe"}


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

    @Slot()
    def reset_stream(self):
        self._headers = []
        self._stdout_buffer = ""
        self._recent_frames.clear()
        self._last_metrics_emit_ts = 0.0

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
            self._handle_csv_line(line)

    def _handle_csv_line(self, line: str):
        row = next(csv.reader(io.StringIO(line)))
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
        frame_times = [s.frame_time_ms for s in self._recent_frames]
        frame_times_sorted = sorted(frame_times)
        p95_index = min(len(frame_times_sorted) - 1, max(0, int(len(frame_times_sorted) * 0.95) - 1))
        avg_frame_time = sum(frame_times) / len(frame_times)
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
        )

    @staticmethod
    def _parse_sample(data: dict[str, str]) -> FrameSample | None:
        process_name = data.get("Application", "")
        process_id = _PresentMonParser._to_int(data.get("ProcessID"))
        frame_time_ms = _PresentMonParser._to_float(data.get("msBetweenPresents")) or _PresentMonParser._to_float(data.get("FrameTime"))
        if frame_time_ms <= 0:
            return None

        return FrameSample(
            timestamp=datetime.now(),
            process_name=process_name,
            process_id=process_id,
            swap_chain=data.get("SwapChainAddress", ""),
            runtime=data.get("Runtime", ""),
            present_mode=data.get("PresentMode", ""),
            sync_interval=_PresentMonParser._to_int(data.get("SyncInterval")),
            allows_tearing=_PresentMonParser._to_bool(data.get("AllowsTearing")),
            frame_time_ms=frame_time_ms,
            cpu_busy_ms=_PresentMonParser._to_float(data.get("msInPresentAPI")),
            cpu_wait_ms=_PresentMonParser._to_float(data.get("msUntilRenderComplete")),
            gpu_busy_ms=_PresentMonParser._to_float(data.get("msGPUActive")),
            displayed_time_ms=_PresentMonParser._to_float(data.get("msUntilDisplayed")),
            raw_fields=data,
        )

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

        self._target_process = ""
        self._target_pid: int | None = None
        self._prefer_process_name_match = False
        self._last_metrics_ts: datetime | None = None
        self._received_frame = False
        self._received_header = False
        self._last_error_text = ""
        self._last_status_text = "Idle"
        self._session_name = "LagLense"
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
        self._parser_thread.start()

    @property
    def executable_path(self) -> Path:
        return self._resolve_executable()

    def is_available(self) -> bool:
        return self._resolve_executable().exists()

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
        self._session_name = f"LagLense-{os.getpid()}-{self._start_count}"
        args = [
            "--output_stdout",
            "--no_console_stats",
            "--v1_metrics",
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
        self._auto_retry_done = False
        self._pending_restart = False
        self._startup_timeout_notified = False
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
        self._process.kill()
        self._set_status("Stopping capture…")

    def restart_capture(self):
        self._pending_restart = True
        self.stop_capture()

    def shutdown(self):
        self._startup_timer.stop()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(1500)
        self._parser_thread.quit()
        self._parser_thread.wait(2000)

    def cleanup_stale_sessions(self, include_active: bool = False) -> int:
        stale = self._find_stale_sessions(force_refresh=True, include_active=include_active)
        for name in stale:
            self._cleanup_named_session(name)
        self._refresh_trace_sessions(force=True)
        if stale:
            self._last_status_text = f"Cleaned {len(stale)} stale trace session(s)"
        else:
            self._last_status_text = "No stale trace sessions found"
        self._emit_diagnostics(force=True)
        return len(stale)

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
                "--v1_metrics",
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

            with temp_path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
                reader = csv.DictReader(fh)
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

    def _read_stdout(self):
        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="ignore")
        if not chunk:
            return
        self._stdout_chunk_received.emit(chunk)

    def _read_stderr(self):
        text = bytes(self._process.readAllStandardError()).decode("utf-8", errors="ignore").strip()
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
                self.error_occurred.emit(
                    "PresentMon failed with ACCESS DENIED. Please restart LagLense as Administrator."
                )
            if "1450" in lowered:
                self.error_occurred.emit(
                    "PresentMon failed with trace session error 1450. Try 'Clean Stale Sessions' first; if it persists, reboot Windows."
                )
                self._set_status("PresentMon trace session error 1450. Waiting for manual cleanup.")
                return
            if "events were lost" in lowered or "30034" in lowered or "18717" in lowered:
                self._set_status("PresentMon warning: ETW events were lost; capture may be incomplete.")
                return
            if "session" in lowered and "different name" in lowered:
                self.error_occurred.emit(
                    f"PresentMon session conflict on {self._session_name}. Will need a fresh restart."
                )
            self._set_status(line[:160])

    @Slot()
    def _on_header_seen(self):
        self._received_header = True
        self._startup_timeout_notified = False
        self._startup_timer.stop()
        self._set_status(f"Capturing {self.target_description()}")

    @Slot(object)
    def _on_sample_parsed(self, sample: FrameSample):
        self._received_frame = True

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
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(2500)
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
            f"trace_total={trace_total} | likely_remaining_budget={'low' if trace_total >= 40 else 'medium' if trace_total >= 25 else 'healthy'} | top={top_summary}\n"
            f"target={self.target_description()} | header_seen={self._received_header} | received_frame={self._received_frame}\n"
            f"status={self._last_status_text}\n"
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
            if not (name.startswith("LagLense-") or name == "PresentMon"):
                continue
            if not include_active and name.lower() == current:
                continue
            stale.append(name)
        return stale

    def _cleanup_named_session(self, session_name: str):
        if not session_name:
            return
        try:
            subprocess.run(
                ["logman", "stop", session_name, "-ets"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            pass

    def _list_trace_sessions(self) -> list[str]:
        try:
            result = subprocess.run(
                ["logman", "query", "-ets"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except Exception:
            return []

        names: list[str] = []
        for line in result.stdout.splitlines():
            stripped = line.rstrip()
            if (
                not stripped
                or stripped.startswith("Data Collector Set")
                or stripped.startswith("---")
                or stripped.startswith("The command completed successfully.")
            ):
                continue
            columns = re.split(r"\s{2,}", stripped.strip())
            if len(columns) >= 3 and columns[-2] == "Trace":
                name = columns[0].strip()
                if name:
                    names.append(name)
                continue
            match = re.match(r"^(?P<name>.+?)\s+Trace\s+Running\s*$", stripped)
            if match:
                name = match.group("name").strip()
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
