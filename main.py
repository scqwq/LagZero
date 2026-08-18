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

from core.collectors import SystemCollector
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
    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("System Lag Detective")
    app.setApplicationVersion("1.0.0")
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray

    # --- Instantiate core components ---
    collector = SystemCollector(interval=1.0, top_n_processes=10)
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
    compat_capture = CompatibilityCapture(interval_ms=500)
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
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
