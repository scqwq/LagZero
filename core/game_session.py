"""
core/game_session.py — Foreground game/window detection.

Keeps track of the current foreground window and offers a curated list of
candidate windows that are large enough to plausibly be games.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from time import monotonic

import psutil
from PySide6.QtCore import QThread, Signal

from core.models import GameSessionInfo, GameWindowCandidate


user32 = ctypes.WinDLL("user32", use_last_error=True)


BLACKLISTED_PROCESSES = {
    "applicationframehost.exe",
    "cmd.exe",
    "conhost.exe",
    "explorer.exe",
    "laglense.exe",
    "python.exe",
    "pythonw.exe",
    "powershell.exe",
    "pwsh.exe",
    "dwm.exe",
    "searchhost.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "discord.exe",
    "msedge.exe",
    "chrome.exe",
    "windowsterminal.exe",
    "openconsole.exe",
    "wezterm-gui.exe",
}

MIN_WINDOW_WIDTH = 640
MIN_WINDOW_HEIGHT = 360
PROCESS_NAME_CACHE_TTL_S = 3.0
_PROCESS_NAME_CACHE: dict[int, tuple[str, float]] = {}


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value.strip()


def _window_rect(hwnd: int) -> tuple[int, int]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return max(0, rect.right - rect.left), max(0, rect.bottom - rect.top)


def _process_name(pid: int) -> str:
    now = monotonic()
    cached = _PROCESS_NAME_CACHE.get(pid)
    if cached and (now - cached[1]) < PROCESS_NAME_CACHE_TTL_S:
        return cached[0]
    try:
        name = psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return ""
    _PROCESS_NAME_CACHE[pid] = (name, now)
    return name


def _candidate_from_hwnd(hwnd: int, foreground_hwnd: int | None = None) -> GameWindowCandidate | None:
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return None

    width, height = _window_rect(hwnd)
    if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    process_name = _process_name(pid.value)
    if not process_name:
        return None

    title = _window_title(hwnd)
    is_foreground = int(hwnd) == int(foreground_hwnd or 0)
    if not title:
        if not is_foreground:
            return None
        title = process_name

    return GameWindowCandidate(
        hwnd=int(hwnd),
        pid=int(pid.value),
        process_name=process_name,
        title=title,
        width=width,
        height=height,
        is_foreground=is_foreground,
    )


def _looks_like_game(candidate: GameWindowCandidate) -> bool:
    name = candidate.process_name.lower()
    if name in BLACKLISTED_PROCESSES:
        return False
    return True


def list_window_candidates() -> list[GameWindowCandidate]:
    foreground = user32.GetForegroundWindow()
    found: list[GameWindowCandidate] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        candidate = _candidate_from_hwnd(hwnd, foreground)
        if candidate and _looks_like_game(candidate):
            found.append(candidate)
        return True

    user32.EnumWindows(_enum, 0)

    # Deduplicate on hwnd while keeping the best candidate for each window.
    deduped: dict[int, GameWindowCandidate] = {}
    ranked = sorted(
        found,
        key=lambda c: (
            c.is_foreground,
            c.width * c.height,
            c.process_name.lower(),
            c.hwnd,
        ),
        reverse=True,
    )
    for item in ranked:
        deduped[item.hwnd] = item
    # Return in a deterministic order so the combo box does not rebuild just
    # because EnumWindows happened to enumerate in a different sequence.
    return sorted(
        deduped.values(),
        key=lambda c: (
            not c.is_foreground,
            -(c.width * c.height),
            c.process_name.lower(),
            c.title.lower(),
            c.hwnd,
        ),
    )


def current_foreground_session() -> GameSessionInfo:
    hwnd = user32.GetForegroundWindow()
    candidate = _candidate_from_hwnd(hwnd, hwnd)
    if candidate and _looks_like_game(candidate):
        return GameSessionInfo(
            pid=candidate.pid,
            process_name=candidate.process_name,
            window_title=candidate.title,
            hwnd=candidate.hwnd,
            width=candidate.width,
            height=candidate.height,
            is_foreground=True,
            source="auto",
        )
    return GameSessionInfo(pid=None, process_name="", window_title="", source="auto")


class GameSessionDetector(QThread):
    """
    Polls the foreground window and emits both the active session and a current
    list of candidate windows. The polling cost is low and avoids injection or
    process-memory access.
    """

    session_changed = Signal(object)
    candidates_changed = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, interval_ms: int = 1000, parent=None):
        super().__init__(parent)
        self.interval_ms = interval_ms
        self._running = False
        self._last_key: tuple[int | None, int | None, str] | None = None
        self._last_candidates_key: tuple[tuple[int, int, str, int, int], ...] | None = None

    def run(self):
        self._running = True
        while self._running:
            try:
                session = current_foreground_session()
                candidates = list_window_candidates()
                session_key = (session.pid, session.hwnd, session.process_name)
                if session_key != self._last_key:
                    self._last_key = session_key
                    self.session_changed.emit(session)
                candidates_key = tuple(
                    (
                        item.hwnd,
                        item.pid,
                        item.process_name,
                        item.width,
                        item.height,
                    )
                    for item in candidates
                )
                if candidates_key != self._last_candidates_key:
                    self._last_candidates_key = candidates_key
                    self.candidates_changed.emit(candidates)
            except Exception as exc:  # noqa: BLE001
                self.error_occurred.emit(str(exc))
            self.msleep(self.interval_ms)

    def stop(self):
        self._running = False
        self.wait(2000)
