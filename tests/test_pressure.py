import unittest

from core.models import ProcessGroupSample, ProcessSample, SystemSample, TargetProcessMetrics
from core.pressure import (
    PressureAlertScheduler,
    PressureEvaluation,
    PressureFinding,
    default_settings,
    evaluate_pressure,
)
from datetime import datetime


class PressureTests(unittest.TestCase):
    def test_default_thresholds_follow_hardware_curves(self):
        settings = default_settings(32, 64.0)
        self.assertAlmostEqual(settings.system_cpu_percent, 15.0 + 10.0 * pow(2.718281828, -2.0), places=5)
        self.assertAlmostEqual(settings.background_process_cpu_percent, 8.0 + 12.0 * pow(2.718281828, -32.0 / 12.0), places=5)
        self.assertAlmostEqual(settings.foreground_process_cpu_percent, 35.0 + 15.0 * pow(2.718281828, -2.0), places=5)
        self.assertAlmostEqual(settings.ram_available_warning_gb, 7.8, places=5)

    def test_pressure_evaluation_reports_machine_share(self):
        settings = default_settings(32, 64.0)
        sample = SystemSample(
            timestamp=datetime.now(),
            cpu_percent=20.0,
            cpu_per_core=[],
            ram_percent=85.0,
            ram_used_mb=54.0 * 1024,
            ram_total_mb=64.0 * 1024,
            swap_percent=0.0,
            responsiveness_ms=5.0,
            ram_available_mb=7.0 * 1024,
            top_processes=[ProcessSample(1, "tool.exe", 320.0, 0.1, cpu_machine_share=10.0)],
            process_groups=[ProcessGroupSample("Microsoft Edge", 8, 10.0, 7.0 * 1024)],
            target_process=TargetProcessMetrics(2, "game.exe", 1200.0, 10.0, cpu_machine_share=40.0),
        )
        evaluation = evaluate_pressure(sample, settings, 32)
        self.assertTrue(any(f.code == "CPU_PRESSURE_RISK" for f in evaluation.findings))
        self.assertTrue(any(f.code == "RAM_PRESSURE_RISK" for f in evaluation.findings))
        self.assertTrue(any(f.code == "FOREGROUND_CPU_PRESSURE" for f in evaluation.findings))
        self.assertTrue(any(f.code == "BACKGROUND_GROUP_CPU_PRESSURE" for f in evaluation.findings))
        self.assertTrue(any(f.code == "BACKGROUND_GROUP_RAM_PRESSURE" for f in evaluation.findings))

    def test_alert_scheduler_uses_backoff_and_recovery(self):
        scheduler = PressureAlertScheduler([15.0, 30.0], onset_seconds=2.0)
        active = PressureEvaluation(
            [PressureFinding("CPU_PRESSURE_RISK", "high", 20.0, 16.0)],
            True,
        )
        inactive = PressureEvaluation([], False)

        self.assertFalse(scheduler.update(active, now=0.0))
        self.assertTrue(scheduler.update(active, now=2.0))
        self.assertFalse(scheduler.update(active, now=16.0))
        self.assertTrue(scheduler.update(active, now=17.0))
        self.assertTrue(scheduler.update(active, now=47.0))
        self.assertFalse(scheduler.update(inactive, now=48.0))
        self.assertEqual(scheduler.state, "recover")
        self.assertFalse(scheduler.update(inactive, now=79.0))
        self.assertEqual(scheduler.state, "normal")
        self.assertFalse(scheduler.update(active, now=80.0))
        self.assertTrue(scheduler.update(active, now=82.0))


if __name__ == "__main__":
    unittest.main()
