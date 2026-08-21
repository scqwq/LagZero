"""
core/elevation.py — Windows UAC self-elevation.

Why the whole app elevates rather than just PresentMon:

PresentMon's embedded manifest is `asInvoker`, so it inherits the parent token.
Requesting elevation for a child process requires ShellExecuteW's "runas" verb,
and ShellExecuteW has no parameters for stream redirection — an elevated child
launched that way cannot hand back the stdout pipe that PresentMonBridge parses.
CreateProcess *can* redirect, but cannot elevate.

So the only way to get an elevated PresentMon whose stdout we can still read is
to elevate LagLense itself and let PresentMon inherit the token.

Elevation matters because without it PresentMon cannot:
  - enable the DxgKrnl real-time providers reliably (ETW events get lost),
  - target processes it cannot query (`--process_name` silently matches nothing),
  - stop a stale real-time session (`logman stop` returns access denied).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys


# Set in the child we spawn, so a failed elevation can never cause a fork bomb.
_ELEVATION_ATTEMPT_ENV = "LAGLENSE_ELEVATION_ATTEMPTED"

# ShellExecuteW returns a value <= 32 to signal failure.
_SHELL_EXECUTE_MIN_SUCCESS = 32
_SW_SHOWNORMAL = 1
_ERROR_CANCELLED = 1223  # user dismissed the UAC prompt


def is_windows() -> bool:
    return os.name == "nt"


def is_elevated() -> bool:
    """True when the current process holds an elevated (administrator) token."""
    if not is_windows():
        # POSIX: treat root as elevated. LagLense is Windows-only in practice,
        # but this keeps the helper honest when imported on other platforms.
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevation_already_attempted() -> bool:
    return os.environ.get(_ELEVATION_ATTEMPT_ENV) == "1"


def _quote(value: str) -> str:
    """
    Quote one argv element for the single command-line string ShellExecuteW takes.

    Follows the MSVCRT rule: a backslash is only an escape character when it
    precedes a quote, so only those runs get doubled. Doubling every backslash
    would turn "C:\\Program Files\\LagLense\\main.py" into a path with literal
    double separators and break relaunch for anyone whose install path has a
    space in it.
    """
    if not value:
        return '""'
    if not any(ch in value for ch in ' \t"'):
        return value
    out = ['"']
    backslashes = 0
    for ch in value:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            # Escape the run that precedes the quote, then the quote itself.
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
        else:
            out.append("\\" * backslashes)
            out.append(ch)
        backslashes = 0
    # A trailing run precedes the closing quote, so it must be escaped too.
    out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


def _relaunch_target() -> tuple[str, str]:
    """
    Resolve (executable, argument string) for relaunching this app.

    Frozen builds (PyInstaller) re-exec the bundled exe directly. Source runs
    re-exec the interpreter with the original script and arguments.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, " ".join(_quote(a) for a in sys.argv[1:])
    return sys.executable, " ".join(_quote(a) for a in sys.argv)


def relaunch_as_admin() -> tuple[bool, str]:
    """
    Re-exec this app elevated via the ShellExecuteW "runas" verb.

    Returns (spawned, message). When spawned is True the caller must exit
    immediately and let the elevated instance take over; the two must not run
    concurrently or they will fight over the same ETW session name.

    Never raises, and never retries: the child is marked via environment so a
    denied or silently-failing elevation cannot spawn processes in a loop.
    """
    if not is_windows():
        return False, "Self-elevation is only supported on Windows."
    if is_elevated():
        return False, "Already running with administrator privileges."
    if elevation_already_attempted():
        return False, "Elevation was already attempted for this session."

    executable, arguments = _relaunch_target()
    # The child reads this and will not try to elevate again.
    os.environ[_ELEVATION_ATTEMPT_ENV] = "1"
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,           # hwnd
            "runas",        # the elevation verb
            executable,
            arguments,
            os.getcwd(),
            _SW_SHOWNORMAL,
        )
    except Exception as exc:
        os.environ.pop(_ELEVATION_ATTEMPT_ENV, None)
        return False, f"Elevation failed: {exc}"

    if result > _SHELL_EXECUTE_MIN_SUCCESS:
        return True, "Restarting LagLense with administrator privileges…"

    os.environ.pop(_ELEVATION_ATTEMPT_ENV, None)
    if result == _ERROR_CANCELLED:
        return False, (
            "Administrator access was declined. High-precision capture needs "
            "elevation; LagLense will continue in compatibility mode."
        )
    return False, f"Elevation failed (ShellExecuteW returned {result})."


def stop_trace_session(session_name: str) -> tuple[bool, str]:
    """
    Stop one real-time ETW session, reporting failures instead of swallowing them.

    'logman stop' requires elevation; unelevated callers get access denied, which
    is precisely why stale-session cleanup used to appear to succeed while doing
    nothing.
    """
    if not session_name:
        return False, "No session name given."
    try:
        result = subprocess.run(
            ["logman", "stop", session_name, "-ets"],
            capture_output=True,
            timeout=10,
            check=False,
            # logman emits localised OEM text; decode explicitly at the call site.
        )
    except Exception as exc:
        return False, f"{session_name}: {exc}"

    if result.returncode == 0:
        return True, f"{session_name}: stopped."
    detail = decode_console(result.stdout) or decode_console(result.stderr)
    detail = " ".join(detail.split())[:160] or f"exit {result.returncode}"
    return False, f"{session_name}: {detail}"


def decode_console(payload: bytes) -> str:
    """Decode console output using the active code page, not a fixed guess."""
    if not payload:
        return ""
    encodings = []
    try:
        encodings.append(f"cp{ctypes.windll.kernel32.GetConsoleOutputCP()}")
    except Exception:
        pass
    encodings.extend(["utf-8", "cp936", "cp1252"])
    for encoding in encodings:
        try:
            return payload.decode(encoding).strip()
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace").strip()
