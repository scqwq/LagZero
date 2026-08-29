import unittest

from datetime import datetime, timedelta
from core.models import (
    FrameAttribution,
    FrameStutterEpisode,
    ProcessGroupSample,
    ProcessSample,
    SystemSample,
    TargetProcessMetrics,
)
from core.pressure import (
    PressureAlertScheduler,
    PressureEvaluation,
    PressureFinding,
    default_settings,
    evaluate_pressure,
    frame_resource_context,
    summarize_pressure_findings,
)


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

    def test_pressure_summary_groups_findings_without_calling_them_stutter(self):
        findings = [
            PressureFinding("CPU_PRESSURE_RISK", "系统 CPU 20.0%。", 20.0, 16.0),
            PressureFinding("FOREGROUND_CPU_PRESSURE", "game.exe 40.0%。", 40.0, 35.0),
            PressureFinding("BACKGROUND_GROUP_CPU_PRESSURE", "Edge 10.0%。", 10.0, 8.5),
        ]
        summary = summarize_pressure_findings(findings)
        self.assertIn("当前尚未检测到明显帧卡顿", summary)
        self.assertIn("系统压力：", summary)
        self.assertIn("前台进程压力：", summary)
        self.assertIn("浏览器进程组压力：", summary)

    def test_frame_context_identifies_underutilized_resources(self):
        settings = default_settings(32, 64.0)
        sample = SystemSample(
            timestamp=datetime.now(),
            cpu_percent=10.0,
            cpu_per_core=[],
            ram_percent=60.0,
            ram_used_mb=38.0 * 1024,
            ram_total_mb=64.0 * 1024,
            swap_percent=0.0,
            responsiveness_ms=5.0,
            ram_available_mb=26.0 * 1024,
            target_process=TargetProcessMetrics(2, "game.exe", 320.0, 8.0, cpu_machine_share=10.0),
        )
        episode = FrameStutterEpisode(
            started_at=datetime.now(),
            ended_at=datetime.now() + timedelta(seconds=1),
            target_process="game.exe",
            event_type="FRAME_STUTTER",
            peak_frame_time_ms=80.0,
            avg_frame_time_ms=30.0,
            p95_frame_time_ms=60.0,
            slow_frame_count=3,
            freeze_frame_count=0,
            peak_cpu_wait_ms=50.0,
            peak_gpu_busy_ms=8.0,
            present_mode="Hardware: Legacy Flip",
            severity=0.8,
            explanation="frame stutter",
            peak_cpu_busy_ms=10.0,
            attribution=FrameAttribution(
                category="DRIVER_RENDER_PATH",
                confidence=0.8,
                cpu_share=0.15,
                gpu_share=0.20,
                wait_share=0.65,
            ),
        )
        context = frame_resource_context(episode, sample, [], settings)
        self.assertIn("等待、锁、单线程依赖", context)
        self.assertIn("不是硬件算力不足", context)

    def test_frame_context_separates_foreground_and_background_pressure(self):
        settings = default_settings(32, 64.0)
        sample = SystemSample(
            timestamp=datetime.now(),
            cpu_percent=25.0,
            cpu_per_core=[],
            ram_percent=70.0,
            ram_used_mb=45.0 * 1024,
            ram_total_mb=64.0 * 1024,
            swap_percent=0.0,
            responsiveness_ms=8.0,
            ram_available_mb=5.0 * 1024,
            target_process=TargetProcessMetrics(2, "game.exe", 1200.0, 10.0, cpu_machine_share=40.0),
        )
        episode = FrameStutterEpisode(
            started_at=datetime.now(),
            ended_at=datetime.now() + timedelta(seconds=1),
            target_process="game.exe",
            event_type="FRAME_STUTTER",
            peak_frame_time_ms=70.0,
            avg_frame_time_ms=30.0,
            p95_frame_time_ms=50.0,
            slow_frame_count=2,
            freeze_frame_count=0,
            peak_cpu_wait_ms=20.0,
            peak_gpu_busy_ms=30.0,
            present_mode="Hardware: Legacy Flip",
            severity=0.7,
            explanation="frame stutter",
        )
        findings = [
            PressureFinding("RAM_PRESSURE_RISK", "可用内存 5.00 GB。", 5.0, 7.8),
            PressureFinding("FOREGROUND_CPU_PRESSURE", "game.exe 40.0%。", 40.0, 35.0),
            PressureFinding("BACKGROUND_GROUP_CPU_PRESSURE", "Edge 10.0%。", 10.0, 8.5),
        ]
        context = frame_resource_context(episode, sample, findings, settings)
        self.assertIn("系统可用内存较低", context)
        self.assertIn("目标进程自身资源占用较高", context)
        self.assertIn("后台程序资源压力", context)


if __name__ == "__main__":
    unittest.main()
