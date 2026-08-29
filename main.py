"""
main.py — Application entry point.

Wires together:
  - SystemCollector (background thread)
  - DetectionEngine
  - SnapshotRecorder
  - CauseAnalyzer
  - Storage
  - MainWindow (UI)

Everything communicates via Qt signals/slots — no shared state, no locks.
"""
import signal
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QThread, QTimer

from core import elevation
from core.collectors import SystemCollector
from core.collectors import machine_cpu_count
from core.pressure import load_settings, select_processes_for_report
from core.detection import DetectionEngine
from core.recorder import SnapshotRecorder
from core.analyzer import CauseAnalyzer
from core.compat_capture import CompatibilityCapture
from core.compat_detector import CompatibilityStutterDetector
from core.frame_detector import FrameStutterDetector
from core.game_session import GameSessionDetector
from core.presentmon_bridge import PresentMonBridge
from core.storage import Storage
from ui.main_window import MainWindow


def main():
    # Elevate before building anything. PresentMon is manifested asInvoker, so it
    # inherits this process's token; without elevation it cannot reliably enable
    # the DxgKrnl providers, cannot target processes via --process_name, and
    # cannot stop stale real-time sessions. Done first so the replaced process
    # never leaves a half-initialised UI or a claimed ETW session behind.
    if not elevation.is_elevated() and not elevation.elevation_already_attempted():
        spawned, message = elevation.relaunch_as_admin()
        print(message)
        if spawned:
            # The elevated instance takes over; two instances would collide on
            # the same ETW session name.
            return 0

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("System Lag Detective")
    app.setApplicationVersion("1.0.0")
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray

    # --- Instantiate core components ---
    import psutil

    pressure_settings = load_settings(
        machine_cpu_count(),
        psutil.virtual_memory().total / (1024 ** 3),
    )
    collector = SystemCollector(
        interval=1.0,
        top_n_processes=32,
        process_selector=lambda processes, target_pid, cpu_count: select_processes_for_report(
            processes,
            pressure_settings,
            target_pid,
            cpu_count,
        ),
    )
    engine    = DetectionEngine()
    recorder  = SnapshotRecorder(pre_lag_seconds=5)
    analyzer  = CauseAnalyzer()
    storage   = Storage()
    session_detector = GameSessionDetector(interval_ms=2000)
    presentmon = PresentMonBridge()
    frame_detector = FrameStutterDetector()
    frame_detector_thread = QThread()
    frame_detector.moveToThread(frame_detector_thread)
    frame_detector_thread.start()
    presentmon.connect_frame_consumer(frame_detector.ingest_frame)
    compat_capture = CompatibilityCapture(interval_ms=400)
    compat_detector = CompatibilityStutterDetector()
    compat_detector_thread = QThread()
    compat_detector.moveToThread(compat_detector_thread)
    compat_detector_thread.start()
    compat_capture.sample_captured.connect(compat_detector.ingest_sample)

    # --- Build UI ---
    window = MainWindow(
        collector=collector,
        engine=engine,
        recorder=recorder,
        analyzer=analyzer,
        storage=storage,
        session_detector=session_detector,
        presentmon=presentmon,
        frame_detector=frame_detector,
        compat_capture=compat_capture,
        compat_detector=compat_detector,
        pressure_settings=pressure_settings,
    )
    window.show()

    def _handle_sigint(_signum, _frame):
        window.quit_application()

    signal.signal(signal.SIGINT, _handle_sigint)
    sigint_pump = QTimer()
    sigint_pump.timeout.connect(lambda: None)
    sigint_pump.start(250)

    # --- Start background collection ---
    collector.start()
    session_detector.start()

    exit_code = app.exec()
    presentmon.stop_capture()
    presentmon.shutdown()
    compat_capture.shutdown()
    frame_detector_thread.quit()
    frame_detector_thread.wait(2000)
    compat_detector_thread.quit()
    compat_detector_thread.wait(2000)
    session_detector.stop()
    collector.stop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
