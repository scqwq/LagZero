"""Pressure thresholds and settings for resource-risk reporting."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from time import monotonic

from core.collectors import BROWSER_PROCESS_GROUPS, per_core_to_machine_share
from core.models import ProcessSample, SystemSample


SETTINGS_PATH = Path(__file__).parent.parent / "data" / "pressure_settings.json"


@dataclass
class PressureSettings:
    allow_foreground_high_usage: bool = True
    system_cpu_percent: float = 0.0
    ram_available_warning_gb: float = 0.0
    background_process_cpu_percent: float = 0.0
    background_total_cpu_percent: float = 0.0
    foreground_process_cpu_percent: float = 0.0
    background_process_ram_gb: float = 0.0
    foreground_process_ram_gb: float = 0.0


@dataclass
class PressureFinding:
    code: str
    message: str
    value: float
    threshold: float

    @property
    def ratio(self) -> float:
        if self.code == "RAM_PRESSURE_RISK":
            return self.threshold / self.value if self.value > 0 else 99.0
        return self.value / self.threshold if self.threshold > 0 else 99.0


@dataclass
class PressureEvaluation:
    findings: list[PressureFinding]
    near_active: bool


class PressureAlertScheduler:
    """Exponential backoff with a memory period for near-threshold pressure."""

    def __init__(self, intervals: list[float], onset_seconds: float = 2.0):
        self.intervals = intervals
        self.onset_seconds = onset_seconds
        self.state = "normal"
        self.level = 0
        self.active_since: float | None = None
        self.last_alert_at: float | None = None
        self.recovery_since: float | None = None

    def update(self, evaluation: PressureEvaluation, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        if evaluation.findings:
            self.recovery_since = None
            if self.state != "active":
                self.state = "active"
                self.active_since = current

            if self.last_alert_at is None:
                if current - self.active_since >= self.onset_seconds:
                    self.last_alert_at = current
                    return True
                return False

            interval = self.intervals[min(self.level, len(self.intervals) - 1)]
            if current - self.last_alert_at >= interval:
                self.last_alert_at = current
                self.level = min(self.level + 1, len(self.intervals) - 1)
                return True
            return False

        if self.state != "normal":
            self.state = "recover"
            if self.recovery_since is None:
                self.recovery_since = current
            interval = self.intervals[min(self.level, len(self.intervals) - 1)]
            if not evaluation.near_active and current - self.recovery_since >= interval * 0.85:
                self.state = "normal"
                self.level = 0
                self.active_since = None
                self.last_alert_at = None
                self.recovery_since = None
        return False

    def reset(self) -> None:
        self.state = "normal"
        self.level = 0
        self.active_since = None
        self.last_alert_at = None
        self.recovery_since = None


def ram_available_warning_gb(total_ram_gb: float) -> float:
    if total_ram_gb <= 16:
        return max(0.5, total_ram_gb * 0.065)
    if total_ram_gb <= 32:
        return 1.0 + (total_ram_gb - 16) * 0.125
    return 3.0 + (total_ram_gb - 32) * 0.15


def background_process_ram_threshold_gb(total_ram_gb: float) -> float:
    return 1.0 + 6.0 * (1.0 - math.exp(-total_ram_gb / 24.0))


def foreground_process_ram_threshold_gb(total_ram_gb: float) -> float:
    return 1.5 + 8.0 * (1.0 - math.exp(-total_ram_gb / 24.0))


def background_process_cpu_threshold_percent(logical_cpu_count: int) -> float:
    return 8.0 + 12.0 * math.exp(-logical_cpu_count / 12.0)


def background_total_cpu_threshold_percent(logical_cpu_count: int) -> float:
    return 15.0 + 10.0 * math.exp(-logical_cpu_count / 16.0)


def foreground_process_cpu_threshold_percent(logical_cpu_count: int) -> float:
    return 35.0 + 15.0 * math.exp(-logical_cpu_count / 16.0)


def default_settings(logical_cpu_count: int, total_ram_gb: float) -> PressureSettings:
    return PressureSettings(
        allow_foreground_high_usage=True,
        system_cpu_percent=background_total_cpu_threshold_percent(logical_cpu_count),
        ram_available_warning_gb=ram_available_warning_gb(total_ram_gb),
        background_process_cpu_percent=background_process_cpu_threshold_percent(logical_cpu_count),
        background_total_cpu_percent=background_total_cpu_threshold_percent(logical_cpu_count),
        foreground_process_cpu_percent=foreground_process_cpu_threshold_percent(logical_cpu_count),
        background_process_ram_gb=background_process_ram_threshold_gb(total_ram_gb),
        foreground_process_ram_gb=foreground_process_ram_threshold_gb(total_ram_gb),
    )


def load_settings(logical_cpu_count: int, total_ram_gb: float) -> PressureSettings:
    fallback = default_settings(logical_cpu_count, total_ram_gb)
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    settings = default_settings(logical_cpu_count, total_ram_gb)
    for field in asdict(settings):
        if field in payload:
            setattr(settings, field, payload[field])
    return settings


def save_settings(settings: PressureSettings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def evaluate_pressure(
    sample: SystemSample,
    settings: PressureSettings,
    logical_cpu_count: int,
) -> PressureEvaluation:
    findings: list[PressureFinding] = []

    if sample.cpu_percent >= settings.system_cpu_percent:
        findings.append(PressureFinding(
            "CPU_PRESSURE_RISK",
            f"系统 CPU 占用 {sample.cpu_percent:.1f}%，超过压力阈值 {settings.system_cpu_percent:.1f}%。",
            sample.cpu_percent,
            settings.system_cpu_percent,
        ))

    available_gb = sample.ram_available_mb / 1024.0
    if available_gb > 0 and available_gb <= settings.ram_available_warning_gb:
        findings.append(PressureFinding(
            "RAM_PRESSURE_RISK",
            f"系统可用内存 {available_gb:.2f} GB，低于警告线 {settings.ram_available_warning_gb:.2f} GB。",
            available_gb,
            settings.ram_available_warning_gb,
        ))

    target_pid = sample.target_process.pid if sample.target_process else None
    target_name = sample.target_process.name if sample.target_process else ""
    if sample.target_process is not None:
        target_cpu = sample.target_process.cpu_machine_share
        target_cpu_threshold = (
            settings.foreground_process_cpu_percent
            if settings.allow_foreground_high_usage
            else settings.background_process_cpu_percent
        )
        if target_cpu >= target_cpu_threshold:
            findings.append(PressureFinding(
                "FOREGROUND_CPU_PRESSURE",
                f"前台进程 {target_name} 占用 {target_cpu:.1f}% 整机 CPU，超过阈值 {target_cpu_threshold:.1f}%。",
                target_cpu,
                target_cpu_threshold,
            ))

        target_ram_gb = sample.target_process.memory_mb / 1024.0
        target_ram_threshold = (
            settings.foreground_process_ram_gb
            if settings.allow_foreground_high_usage
            else settings.background_process_ram_gb
        )
        if target_ram_gb >= target_ram_threshold:
            findings.append(PressureFinding(
                "FOREGROUND_RAM_PRESSURE",
                f"前台进程 {target_name} 占用 {target_ram_gb:.2f} GB 内存，超过阈值 {target_ram_threshold:.2f} GB。",
                target_ram_gb,
                target_ram_threshold,
            ))

    for group in sample.process_groups:
        if group.cpu_machine_share >= settings.background_process_cpu_percent:
            findings.append(PressureFinding(
                "BACKGROUND_GROUP_CPU_PRESSURE",
                f"{group.name} 共 {group.process_count} 个进程，占用 {group.cpu_machine_share:.1f}% 整机 CPU。",
                group.cpu_machine_share,
                settings.background_process_cpu_percent,
            ))
        group_ram_gb = group.memory_mb / 1024.0
        if group_ram_gb >= settings.background_process_ram_gb:
            findings.append(PressureFinding(
                "BACKGROUND_GROUP_RAM_PRESSURE",
                f"{group.name} 共 {group.process_count} 个进程，占用 {group_ram_gb:.2f} GB 私有内存。",
                group_ram_gb,
                settings.background_process_ram_gb,
            ))

    for process in sample.top_processes:
        if process.pid == target_pid:
            continue
        if (process.name or "").lower() in BROWSER_PROCESS_GROUPS:
            continue
        process_cpu = process.cpu_machine_share
        if process_cpu >= settings.background_process_cpu_percent:
            findings.append(PressureFinding(
                "BACKGROUND_PROCESS_CPU_PRESSURE",
                f"后台进程 {process.name}（PID {process.pid}）占用 {process_cpu:.1f}% 整机 CPU。",
                process_cpu,
                settings.background_process_cpu_percent,
            ))
        process_ram_gb = process.memory_mb / 1024.0
        if process_ram_gb >= settings.background_process_ram_gb:
            findings.append(PressureFinding(
                "BACKGROUND_PROCESS_RAM_PRESSURE",
                f"后台进程 {process.name}（PID {process.pid}）占用 {process_ram_gb:.2f} GB 内存。",
                process_ram_gb,
                settings.background_process_ram_gb,
            ))

    near_active = any(finding.ratio >= 0.85 for finding in findings)
    near_findings = _near_pressure(sample, settings, logical_cpu_count, target_pid)
    return PressureEvaluation(findings=findings, near_active=near_active or near_findings)


def _near_pressure(
    sample: SystemSample,
    settings: PressureSettings,
    logical_cpu_count: int,
    target_pid: int | None,
) -> bool:
    if sample.cpu_percent >= settings.system_cpu_percent * 0.85:
        return True
    available_gb = sample.ram_available_mb / 1024.0
    if 0 < available_gb <= settings.ram_available_warning_gb / 0.85:
        return True
    for process in sample.top_processes:
        if process.pid == target_pid or (process.name or "").lower() in BROWSER_PROCESS_GROUPS:
            continue
        if process.cpu_machine_share >= settings.background_process_cpu_percent * 0.85:
            return True
    for group in sample.process_groups:
        if group.cpu_machine_share >= settings.background_process_cpu_percent * 0.85:
            return True
        if group.memory_mb / 1024.0 >= settings.background_process_ram_gb * 0.85:
            return True
    return False


def select_processes_for_report(
    processes: list[ProcessSample],
    settings: PressureSettings,
    target_pid: int | None,
    logical_cpu_count: int,
    limit: int = 32,
) -> list[ProcessSample]:
    """Keep threshold-exceeding processes; otherwise retain the top three."""
    rated: list[tuple[float, ProcessSample]] = []
    all_rated: list[tuple[float, ProcessSample]] = []
    for process in processes:
        if (process.name or "").lower() in BROWSER_PROCESS_GROUPS:
            continue
        cpu_share = per_core_to_machine_share(process.cpu_percent)
        process.cpu_machine_share = cpu_share
        is_target = process.pid == target_pid
        cpu_threshold = (
            settings.foreground_process_cpu_percent
            if is_target and settings.allow_foreground_high_usage
            else settings.background_process_cpu_percent
        )
        ram_threshold = (
            settings.foreground_process_ram_gb
            if is_target and settings.allow_foreground_high_usage
            else settings.background_process_ram_gb
        )
        cpu_ratio = cpu_share / cpu_threshold if cpu_threshold > 0 else 0.0
        ram_ratio = (process.memory_mb / 1024.0) / ram_threshold if ram_threshold > 0 else 0.0
        rating = max(cpu_ratio, ram_ratio, 1.0 if is_target else 0.0)
        all_rated.append((rating, process))
        if rating >= 1.0:
            rated.append((rating, process))

    rated.sort(key=lambda item: item[0], reverse=True)
    selected = [process for _, process in rated[:limit]]
    if selected:
        return selected

    all_rated.sort(key=lambda item: item[0], reverse=True)
    return [process for _, process in all_rated[:3]]
