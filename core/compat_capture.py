"""
core/compat_capture.py — Compatibility capture backend.

Fallback sampler for machines where PresentMon cannot attach reliably.
It focuses on window responsiveness plus target-process resource spikes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
from time import monotonic

import psutil
from PySide6.QtCore import QThread, Signal

from core.models import CompatibilityMetricsSnapshot, CompatibilitySample


user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_size_t)
COLORREF = getattr(wintypes, "COLORREF", wintypes.DWORD)

WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002
RESPONSIVENESS_TIMEOUT_MS = 250
STATUS_EMIT_INTERVAL_S = 1.0


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ULONG_PTR),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
user32.IsHungAppWindow.restype = wintypes.BOOL
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = COLORREF


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value.strip()


def _window_area(hwnd: int) -> int:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)


def _find_window_for_target(process_id: int | None, process_name: str) -> tuple[int, str]:
    process_name = (process_name or "").lower()
    best_hwnd = 0
    best_title = ""
    best_area = -1

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        nonlocal best_hwnd, best_title, best_area
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        if process_id and pid.value != process_id:
            return True
        if not process_id and process_name:
            try:
                if psutil.Process(pid.value).name().lower() != process_name:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return True
        area = _window_area(hwnd)
        if area > best_area:
            best_hwnd = int(hwnd)
            best_title = title
            best_area = area
        return True

    user32.EnumWindows(_enum, 0)
    return best_hwnd, best_title


class CompatibilityCapture(QThread):
    sample_captured = Signal(object)
    metrics_updated = Signal(object)
    status_changed = Signal(str)
    error_occurred = Signal(str)
    mode_changed = Signal(str)

    def __init__(self, interval_ms: int = 250, parent=None):
        super().__init__(parent)
        self.interval_ms = interval_ms
        self._running = False
        self._active = False
        self._target_process = ""
        self._target_pid: int | None = None
        self._last_io: tuple[int, int, float] | None = None
        self._last_status_text = ""
        self._last_status_emit_ts = 0.0
        self._last_visual_hash = 0
        self._visual_frozen_streak = 0
        self._cached_hwnd = 0
        self._cached_title = ""
        self._cached_hwnd_pid: int | None = None

    def set_target(self, process_name: str = "", pid: int | None = None):
        self._target_process = (process_name or "").strip()
        self._target_pid = pid if pid and pid > 0 else None
        self._last_io = None
        self._last_visual_hash = 0
        self._visual_frozen_streak = 0
        self._cached_hwnd = 0
        self._cached_title = ""
        self._cached_hwnd_pid = None

    def start_capture(self):
        if not self.isRunning():
            self._running = True
            self.start()
        self._active = True
        self.mode_changed.emit("Compatibility")
        self._emit_status(f"Compatibility capture active for {self.target_description()}", force=True)

    def stop_capture(self):
        self._active = False
        self._emit_status("Compatibility capture idle", force=True)

    def shutdown(self):
        self._active = False
        self._running = False
        self.wait(2000)

    def target_description(self) -> str:
        if self._target_pid:
            return f"PID {self._target_pid} ({self._target_process or 'unknown'})"
        return self._target_process or "No target"

    def run(self):
        while self._running:
            if self._active:
                try:
                    sample = self._capture_sample()
                    if sample is not None:
                        self.sample_captured.emit(sample)
                        self.metrics_updated.emit(self._build_metrics(sample))
                except Exception as exc:  # noqa: BLE001
                    self.error_occurred.emit(str(exc))
            self.msleep(self.interval_ms)

    def _capture_sample(self) -> CompatibilitySample | None:
        process = self._resolve_process()
        if process is None:
            self._cached_hwnd = 0
            self._cached_title = ""
            self._cached_hwnd_pid = None
            self._emit_status(f"Compatibility mode waiting for {self.target_description()}")
            return None

        hwnd, title = self._resolve_window_handle(process)
        foreground_hwnd = int(user32.GetForegroundWindow() or 0)
        is_foreground = bool(hwnd and hwnd == foreground_hwnd)
        is_hung = bool(hwnd and user32.IsHungAppWindow(hwnd))
        response_time_ms = self._measure_response_ms(hwnd)
        visual_hash = self._sample_visual_hash(hwnd)
        visual_change_ratio = 0.0
        if visual_hash and self._last_visual_hash:
            visual_change_ratio = 0.0 if visual_hash == self._last_visual_hash else 1.0
        if visual_hash and visual_hash == self._last_visual_hash:
            self._visual_frozen_streak += 1
        else:
            self._visual_frozen_streak = 0
        if visual_hash:
            self._last_visual_hash = visual_hash

        try:
            cpu_percent = process.cpu_percent(interval=None)
            memory_mb = process.memory_info().rss / (1024 * 1024)
            threads = process.num_threads()
            io_counters = process.io_counters()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

        read_kb_s = 0.0
        write_kb_s = 0.0
        now = monotonic()
        current_io = (io_counters.read_bytes, io_counters.write_bytes, now)
        if self._last_io is not None:
            prev_read, prev_write, prev_ts = self._last_io
            elapsed = max(now - prev_ts, 0.001)
            read_kb_s = max(0.0, (current_io[0] - prev_read) / 1024.0 / elapsed)
            write_kb_s = max(0.0, (current_io[1] - prev_write) / 1024.0 / elapsed)
        self._last_io = current_io
        self._emit_status(
            f"Compatibility capture monitoring {process.name()}"
        )

        return CompatibilitySample(
            timestamp=datetime.now(),
            target_process=process.name(),
            process_id=process.pid,
            hwnd=hwnd,
            window_title=title,
            is_foreground=is_foreground,
            is_hung=is_hung,
            response_time_ms=response_time_ms,
            process_cpu_percent=cpu_percent,
            process_memory_mb=memory_mb,
            process_read_kb_s=read_kb_s,
            process_write_kb_s=write_kb_s,
            thread_count=threads,
            visual_hash=visual_hash,
            visual_change_ratio=visual_change_ratio,
            visual_frozen_streak=self._visual_frozen_streak,
        )

    def _resolve_process(self) -> psutil.Process | None:
        if self._target_pid:
            try:
                return psutil.Process(self._target_pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return None
        target_name = self._target_process.lower()
        if not target_name:
            return None
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info.get("name") or "").lower() == target_name:
                    self._target_pid = process.pid
                    return psutil.Process(process.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None

    def _resolve_window_handle(self, process: psutil.Process) -> tuple[int, str]:
        cached_hwnd = self._cached_hwnd
        if cached_hwnd and self._cached_hwnd_pid == process.pid:
            if user32.IsWindow(cached_hwnd) and user32.IsWindowVisible(cached_hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(cached_hwnd, ctypes.byref(pid))
                if pid.value == process.pid:
                    title = _window_title(cached_hwnd)
                    if title:
                        self._cached_title = title
                        return cached_hwnd, title
        hwnd, title = _find_window_for_target(process.pid, process.name())
        self._cached_hwnd = hwnd
        self._cached_title = title
        self._cached_hwnd_pid = process.pid if hwnd else None
        return hwnd, title

    def _measure_response_ms(self, hwnd: int) -> float:
        if not hwnd:
            return 0.0
        result = ULONG_PTR()
        started = monotonic()
        ok = user32.SendMessageTimeoutW(
            hwnd,
            WM_NULL,
            0,
            0,
            SMTO_ABORTIFHUNG,
            RESPONSIVENESS_TIMEOUT_MS,
            ctypes.byref(result),
        )
        elapsed_ms = (monotonic() - started) * 1000.0
        if not ok:
            return float(RESPONSIVENESS_TIMEOUT_MS)
        return elapsed_ms

    def _sample_visual_hash(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0
        width = max(0, rect.right - rect.left)
        height = max(0, rect.bottom - rect.top)
        if width < 4 or height < 4:
            return 0
        hdc = user32.GetWindowDC(hwnd)
        if not hdc:
            return 0
        try:
            points = (
                (width // 6, height // 6),
                (width // 2, height // 6),
                (width * 5 // 6, height // 6),
                (width // 6, height // 2),
                (width // 2, height // 2),
                (width * 5 // 6, height // 2),
                (width // 6, height * 5 // 6),
                (width // 2, height * 5 // 6),
                (width * 5 // 6, height * 5 // 6),
            )
            value = 1469598103934665603
            for x, y in points:
                pixel = int(gdi32.GetPixel(hdc, x, y))
                value ^= pixel & 0xFFFFFFFF
                value *= 1099511628211
                value &= 0xFFFFFFFFFFFFFFFF
            return int(value)
        finally:
            user32.ReleaseDC(hwnd, hdc)

    @staticmethod
    def _build_metrics(sample: CompatibilitySample) -> CompatibilityMetricsSnapshot:
        return CompatibilityMetricsSnapshot(
            updated_at=sample.timestamp,
            target_process=sample.target_process,
            process_id=sample.process_id,
            response_time_ms=sample.response_time_ms,
            process_cpu_percent=sample.process_cpu_percent,
            process_memory_mb=sample.process_memory_mb,
            process_read_kb_s=sample.process_read_kb_s,
            process_write_kb_s=sample.process_write_kb_s,
            thread_count=sample.thread_count,
            is_hung=sample.is_hung,
            visual_hash=sample.visual_hash,
            visual_change_ratio=sample.visual_change_ratio,
            visual_frozen_streak=sample.visual_frozen_streak,
        )

    def _emit_status(self, message: str, force: bool = False):
        now = monotonic()
        if not force and message == self._last_status_text:
            return
        if not force and (now - self._last_status_emit_ts) < STATUS_EMIT_INTERVAL_S:
            return
        self._last_status_text = message
        self._last_status_emit_ts = now
        self.status_changed.emit(message)
