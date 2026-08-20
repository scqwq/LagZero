"""
ui/detail_panel.py — Right panel shown when a lag event is selected.

Rendered as a single rich-text view to keep report switching lightweight.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextBrowser

from core.models import LagEvent, LagSnapshot

GREEN = "#2ecc71"
AMBER = "#f39c12"
RED = "#e74c3c"
BG = "#0d1117"
BG2 = "#161b22"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
PURPLE = "#9b59b6"

CAUSE_ICONS = {
    "REPORT_PENDING": "⏳",
    "CPU_SPIKE": "🔥",
    "CPU_BOUND": "🔥",
    "GPU_BOUND": "🎮",
    "VRAM_PRESSURE": "🧠",
    "DRIVER_RENDER_PATH": "🪟",
    "RAM_EXHAUSTION": "💾",
    "RAM_PRESSURE": "⚠️",
    "SYSTEM_RAM_PRESSURE": "💾",
    "GAME_MEMORY_LIMIT": "📦",
    "BACKGROUND_CLUSTER": "🐝",
    "BACKGROUND_INTERFERENCE": "🐝",
    "DISK_IO": "💿",
    "IO_STALL": "💿",
    "SCHEDULER_CONTENTION": "⚙️",
    "LOCAL_STUTTER": "📉",
    "UNDETERMINED": "❓",
    "FRAME_SPIKE": "📉",
    "FRAME_STUTTER": "🎞️",
    "FRAME_FREEZE": "🧊",
    "Window Not Responding": "🧊",
    "Visual Freeze": "🖼️",
    "Responsiveness Stall": "⏱️",
    "CPU Pressure Stall": "🔥",
    "I/O Pressure Stall": "💿",
    "UNKNOWN": "❓",
}

CAUSE_COLOURS = {
    "REPORT_PENDING": ACCENT,
    "CPU_SPIKE": RED,
    "CPU_BOUND": RED,
    "GPU_BOUND": ACCENT,
    "VRAM_PRESSURE": "#1abc9c",
    "DRIVER_RENDER_PATH": ACCENT,
    "RAM_EXHAUSTION": PURPLE,
    "RAM_PRESSURE": AMBER,
    "SYSTEM_RAM_PRESSURE": RED,
    "GAME_MEMORY_LIMIT": PURPLE,
    "BACKGROUND_CLUSTER": AMBER,
    "BACKGROUND_INTERFERENCE": AMBER,
    "DISK_IO": "#1abc9c",
    "IO_STALL": "#1abc9c",
    "SCHEDULER_CONTENTION": MUTED,
    "LOCAL_STUTTER": AMBER,
    "UNDETERMINED": MUTED,
    "FRAME_SPIKE": ACCENT,
    "FRAME_STUTTER": "#e67e22",
    "FRAME_FREEZE": RED,
    "Window Not Responding": RED,
    "Visual Freeze": ACCENT,
    "Responsiveness Stall": AMBER,
    "CPU Pressure Stall": RED,
    "I/O Pressure Stall": "#1abc9c",
    "UNKNOWN": MUTED,
}

CAUSE_LABELS_ZH = {
    "REPORT_PENDING": "正在生成报告",
    "CPU_SPIKE": "单进程 CPU 峰值",
    "CPU_BOUND": "CPU 瓶颈",
    "GPU_BOUND": "GPU 瓶颈",
    "VRAM_PRESSURE": "显存压力",
    "DRIVER_RENDER_PATH": "驱动 / 渲染链路异常",
    "RAM_EXHAUSTION": "内存耗尽",
    "RAM_PRESSURE": "内存压力过高",
    "SYSTEM_RAM_PRESSURE": "系统内存不足",
    "GAME_MEMORY_LIMIT": "游戏内存分配受限",
    "BACKGROUND_CLUSTER": "后台进程堆积",
    "BACKGROUND_INTERFERENCE": "后台进程干扰",
    "DISK_IO": "磁盘 / IO 瓶颈",
    "IO_STALL": "IO 阻塞",
    "SCHEDULER_CONTENTION": "调度竞争",
    "LOCAL_STUTTER": "本地卡顿",
    "UNDETERMINED": "未确定类型",
    "FRAME_SPIKE": "帧时间尖峰",
    "FRAME_STUTTER": "帧时间卡顿",
    "FRAME_FREEZE": "画面冻结",
    "Window Not Responding": "窗口未响应",
    "Visual Freeze": "画面冻结",
    "Responsiveness Stall": "响应延迟卡顿",
    "CPU Pressure Stall": "CPU 压力卡顿",
    "I/O Pressure Stall": "IO 压力卡顿",
    "UNKNOWN": "未明确分类",
}


def _sparkline(values: list[float], width: int = 20) -> str:
    if not values:
        return "─" * width
    chars = " ▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    span = mx - mn or 1
    return "".join(chars[int((v - mn) / span * (len(chars) - 1))] for v in values[-width:])


class DetailPanelWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._report_language = "zh"
        self._current_event: LagEvent | None = None
        self._current_snapshot: LagSnapshot | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 0, 0, 0)
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setOpenLinks(False)
        self._browser.setFrameShape(QTextBrowser.Shape.NoFrame)
        self._browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._browser.setStyleSheet(
            f"""
            QTextBrowser {{
                background: {BG};
                color: {TEXT};
                border: none;
                padding: 0;
            }}
            """
        )
        layout.addWidget(self._browser)
        self.clear_event()

    def clear_event(self):
        self._current_event = None
        self._current_snapshot = None
        self._browser.setHtml(self._placeholder_html())

    def show_loading_event(self, event: LagEvent):
        self._current_event = event
        self._current_snapshot = None
        code = self._cause_label(event.cause_code or "UNKNOWN")
        title = "Loading lag report..." if self._report_language == "en" else "正在加载卡顿详情…"
        self._browser.setHtml(
            f"""
            <div style="margin-top:120px;text-align:center;color:{MUTED};">
              <div style="font-size:16px;font-weight:700;">{title}</div>
              <div style="margin-top:10px;font-size:13px;">{event.started_at.strftime('%H:%M:%S')}  {code}</div>
            </div>
            """
        )

    def set_report_language(self, language: str):
        if language not in {"zh", "en"} or language == self._report_language:
            return
        self._report_language = language
        if self._current_event is None:
            self.clear_event()
            return
        self.show_event(self._current_event, self._current_snapshot)

    def show_event(self, event: LagEvent, snapshot: LagSnapshot | None):
        self._current_event = event
        self._current_snapshot = snapshot
        code = event.category or event.cause_code or "UNKNOWN"
        colour = CAUSE_COLOURS.get(code, MUTED)
        icon = CAUSE_ICONS.get(code, "❓")
        title = self._cause_label(code)
        scope = self._scope_label(event.scope)
        if event.is_pending:
            duration = "Generating..." if self._report_language == "en" else "正在生成报告"
        else:
            duration = f"{round(event.duration_seconds, 1)}s"

        html = [
            f'<div style="background:{BG2};border-left:3px solid {colour};border-radius:6px;padding:14px 16px;">',
            f'<div style="color:{colour};font-size:15px;font-weight:700;">{icon} {title}</div>',
            f'<div style="color:{MUTED};font-size:12px;margin-top:6px;">{event.started_at.strftime("%A, %b %d at %H:%M:%S")} · {duration} · {scope}</div>',
            f'<div style="color:{TEXT};font-size:13px;margin-top:10px;line-height:1.45;">{self._escape(self._explanation_text(event, snapshot))}</div>',
            "</div>",
        ]

        if snapshot is not None:
            html.append(self._section_html("Peak Metrics" if self._report_language == "en" else "峰值指标"))
            html.append(self._metrics_html(event, snapshot))
            raw_metrics = self._raw_metrics_html(snapshot)
            if raw_metrics:
                html.append(self._section_html("Raw Game Metrics" if self._report_language == "en" else "游戏原始指标"))
                html.append(raw_metrics)
            if snapshot.pre_lag_samples:
                html.append(self._section_html("Pre-Lag Timeline (Last 5s)" if self._report_language == "en" else "卡顿前时间线（最近 5 秒）"))
                html.append(self._timeline_html(snapshot))
            if snapshot.top_processes:
                html.append(self._section_html("Top Processes At Peak" if self._report_language == "en" else "峰值时刻主要进程"))
                html.append(self._processes_html(snapshot))

        self._browser.setHtml("".join(html))

    def _section_html(self, title: str) -> str:
        return f'<div style="margin-top:16px;color:{MUTED};font-size:11px;font-weight:700;border-bottom:1px solid #30363d;padding-bottom:4px;">{title.upper()}</div>'

    def _metrics_html(self, event: LagEvent, snapshot: LagSnapshot) -> str:
        items = [
            ("CPU", f"{snapshot.peak_cpu:.0f}%", RED if snapshot.peak_cpu > 80 else AMBER if snapshot.peak_cpu > 60 else GREEN),
            ("RAM", f"{snapshot.peak_ram:.0f}%", RED if snapshot.peak_ram > 88 else AMBER if snapshot.peak_ram > 70 else GREEN),
            ("RESPONSE", f"{snapshot.peak_responsiveness_ms:.1f} ms", RED if snapshot.peak_responsiveness_ms > 50 else AMBER if snapshot.peak_responsiveness_ms > 20 else GREEN),
            ("SCORE", f"{event.peak_composite_score * 100:.0f}%", RED if event.peak_composite_score > 0.75 else AMBER),
        ]
        cells = []
        for label, value, colour in items:
            cells.append(
                f'<td style="background:#21262d;border-radius:6px;padding:10px 14px;">'
                f'<div style="color:{MUTED};font-size:10px;font-weight:600;">{label}</div>'
                f'<div style="color:{colour};font-size:19px;font-weight:700;margin-top:4px;">{value}</div>'
                f"</td>"
                )
        return f'<table cellspacing="8" cellpadding="0" style="margin-top:8px;"><tr>{"".join(cells)}</tr></table>'

    def _raw_metrics_html(self, snapshot: LagSnapshot) -> str:
        target = snapshot.peak_sample.target_process
        gpu = snapshot.peak_sample.gpu_memory
        if target is None and gpu is None:
            return ""

        sections: list[str] = []
        if target is not None:
            title = "Target Process" if self._report_language == "en" else "目标进程"
            rows = [
                self._raw_metric_row("Name" if self._report_language == "en" else "名称", self._escape(target.name)),
                self._raw_metric_row("PID", str(target.pid)),
                self._raw_metric_row("CPU", f"{target.cpu_percent:.1f}%"),
                self._raw_metric_row("Working Set" if self._report_language == "en" else "进程内存", f"{target.memory_mb:.1f} MB"),
                self._raw_metric_row("Private Memory", f"{target.private_memory_mb:.1f} MB"),
                self._raw_metric_row("Read Throughput" if self._report_language == "en" else "读取吞吐", f"{target.read_kb_s:.1f} KB/s"),
                self._raw_metric_row("Write Throughput" if self._report_language == "en" else "写入吞吐", f"{target.write_kb_s:.1f} KB/s"),
                self._raw_metric_row("Threads" if self._report_language == "en" else "线程数", str(target.thread_count)),
            ]
            sections.append(self._raw_metric_block(title, rows))

        if gpu is not None:
            title = "GPU Memory Budget" if self._report_language == "en" else "显存预算"
            rows = [
                self._raw_metric_row("Local Usage" if self._report_language == "en" else "本地显存占用", f"{gpu.local_usage_mb:.1f} MB"),
                self._raw_metric_row("Local Budget" if self._report_language == "en" else "本地显存预算", f"{gpu.local_budget_mb:.1f} MB"),
                self._raw_metric_row("Local Usage Ratio" if self._report_language == "en" else "本地显存预算占比", self._format_ratio(gpu.local_usage_ratio)),
                self._raw_metric_row("Shared Usage" if self._report_language == "en" else "共享显存占用", f"{gpu.shared_usage_mb:.1f} MB"),
                self._raw_metric_row("Shared Budget" if self._report_language == "en" else "共享显存预算", f"{gpu.shared_budget_mb:.1f} MB"),
                self._raw_metric_row("Shared Usage Ratio" if self._report_language == "en" else "共享显存预算占比", self._format_ratio(gpu.shared_usage_ratio)),
            ]
            sections.append(self._raw_metric_block(title, rows))

        return f'<div style="margin-top:8px;">{"".join(sections)}</div>'

    def _timeline_html(self, snapshot: LagSnapshot) -> str:
        rows = [
            ("CPU", [s.cpu_percent for s in snapshot.pre_lag_samples], "%", RED),
            ("RAM", [s.ram_percent for s in snapshot.pre_lag_samples], "%", PURPLE),
            ("RESP", [s.responsiveness_ms for s in snapshot.pre_lag_samples], "ms", ACCENT),
        ]
        html_rows = []
        for label, values, unit, colour in rows:
            html_rows.append(
                f"<tr>"
                f'<td style="color:{MUTED};font-size:11px;font-weight:700;padding:4px 10px 4px 0;">{label}</td>'
                f'<td style="color:{colour};font-family:Consolas,monospace;font-size:14px;padding:4px 10px;">{_sparkline(values, width=16)}</td>'
                f'<td style="color:{colour};font-size:11px;font-weight:600;padding:4px 0;">{max(values):.0f}{unit}</td>'
                f"</tr>"
            )
        return f'<div style="background:{BG2};border-radius:6px;padding:12px 14px;margin-top:8px;"><table cellspacing="0" cellpadding="0">{"".join(html_rows)}</table></div>'

    def _processes_html(self, snapshot: LagSnapshot) -> str:
        headers = ("PROCESS", "PID", "CPU", "RAM") if self._report_language == "en" else ("进程", "PID", "CPU", "内存")
        rows = [
            f"<tr>"
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[0]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[1]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[2]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 0 8px 0;">{headers[3]}</td>'
            f"</tr>"
        ]
        for proc in snapshot.top_processes[:8]:
            cpu_colour = RED if proc.cpu_percent > 50 else AMBER if proc.cpu_percent > 20 else TEXT
            rows.append(
                f"<tr>"
                f'<td style="color:{TEXT};font-size:12px;padding:4px 12px 4px 0;">{self._escape(proc.name[:28])}</td>'
                f'<td style="color:{MUTED};font-size:12px;padding:4px 12px 4px 0;">{proc.pid}</td>'
                f'<td style="color:{cpu_colour};font-size:12px;font-weight:700;padding:4px 12px 4px 0;">{proc.cpu_percent:.1f}%</td>'
                f'<td style="color:{MUTED};font-size:12px;padding:4px 0;">{proc.memory_mb:.0f} MB</td>'
                f"</tr>"
            )
        return f'<div style="background:{BG2};border-radius:6px;padding:12px 14px;margin-top:8px;"><table cellspacing="0" cellpadding="0">{"".join(rows)}</table></div>'

    def _placeholder_html(self) -> str:
        text = self._placeholder_text()
        return (
            f'<div style="margin-top:120px;text-align:center;color:{MUTED};font-size:14px;">'
            f'{text.replace(chr(10), "<br>")}</div>'
        )

    def _placeholder_text(self) -> str:
        if self._report_language == "en":
            return "<- Select a lag event on the left\nfor the full diagnostic report"
        return "← 选择左侧卡顿事件\n查看完整诊断信息"

    def _cause_label(self, code: str) -> str:
        normalized = code or "UNKNOWN"
        if self._report_language == "en":
            return normalized.replace("_", " ")
        return CAUSE_LABELS_ZH.get(normalized, normalized)

    def _explanation_text(self, event: LagEvent, snapshot: LagSnapshot | None) -> str:
        if self._report_language == "en":
            if event.is_pending:
                return "This stutter is still in progress. The final report will be filled in after the game recovers."
            return event.cause or "No explanation available."
        code = event.category or event.cause_code or "UNKNOWN"
        if event.is_pending:
            return "这次卡顿仍在进行中。为了避免正在生成报告本身拖慢界面，当前先显示轻量占位，等恢复后再补全完整结论。"
        if snapshot is None:
            return event.cause or "暂无详细说明。"
        top_proc = snapshot.top_processes[0] if snapshot.top_processes else None
        if code in {"CPU_SPIKE", "CPU_BOUND"} and top_proc is not None:
            return f"检测到进程 {top_proc.name} (PID {top_proc.pid}) 的 CPU 占用达到 {top_proc.cpu_percent:.1f}%，很可能它是导致本次卡顿的主要原因。"
        if code == "GPU_BOUND":
            return "本次卡顿更像 GPU 侧渲染压力过高导致的帧时间抖动。通常意味着显卡负载、分辨率、特效或驱动路径成为瓶颈。"
        if code == "VRAM_PRESSURE":
            return f"系统公开 GPU 统计显示，显存预算使用率已经非常高。这类卡顿常见于材质、贴图、分辨率或特效导致的显存压力，而不只是单纯算力不够。"
        if code in {"RAM_EXHAUSTION", "RAM_PRESSURE", "SYSTEM_RAM_PRESSURE"}:
            return f"系统内存压力较高，峰值内存占用约为 {snapshot.peak_ram:.0f}%。游戏可用内存余量不足时，容易出现加载慢、卡顿和帧时间抖动。"
        if code == "GAME_MEMORY_LIMIT":
            target = snapshot.peak_sample.target_process
            if target is not None:
                return f"系统总内存并没有吃满，但游戏进程 {target.name} 的内存长期贴近一个较窄上限，这更像游戏自身、运行时或启动参数限制了它可实际使用的内存。"
            return "系统总内存并没有吃满，但目标游戏进程像是被限制在较窄的内存使用上限内。"
        if code in {"BACKGROUND_CLUSTER", "BACKGROUND_INTERFERENCE"}:
            return "检测到多个后台进程同时占用资源，没有单一元凶，但它们叠加后很可能挤占了游戏的 CPU 时间片。"
        if code in {"DISK_IO", "IO_STALL"}:
            return f"本次卡顿更像是磁盘或 IO 瓶颈。峰值系统响应延迟达到 {snapshot.peak_responsiveness_ms:.1f} ms，而这类问题常见于加载、解压、杀毒扫描或分页。"
        if code == "DRIVER_RENDER_PATH":
            return "本次卡顿更像驱动、桌面合成、覆盖层或渲染链路异常，而不是典型的 CPU、内存或磁盘瓶颈。这个结论会保持保守，因为当前没有直接读取驱动内部状态。"
        if code in {"SCHEDULER_CONTENTION", "LOCAL_STUTTER"}:
            return f"系统处于综合性压力状态。峰值 CPU {snapshot.peak_cpu:.0f}%，响应延迟 {snapshot.peak_responsiveness_ms:.1f} ms，暂未识别出唯一的根因。"
        if code in {"FRAME_SPIKE", "FRAME_STUTTER", "FRAME_FREEZE"}:
            return f"这是一次以帧时间异常为主的卡顿事件，持续约 {event.duration_seconds:.1f} 秒。当前报告更偏向玩家看到的结果，而不是完整硬件根因。"
        if code == "Window Not Responding":
            return "游戏窗口在这段时间内出现了未响应现象，通常意味着主线程被阻塞，或游戏没有及时处理窗口消息。"
        if code == "Visual Freeze":
            return "检测到画面长时间不变化，属于视觉冻结。它可能来自渲染线程停顿、资源加载阻塞，或驱动层等待。"
        return event.cause or "暂无详细说明。"

    def _scope_label(self, scope: str) -> str:
        normalized = (scope or "UNDETERMINED").upper()
        if self._report_language == "en":
            return {
                "LOCAL": "Likely local stutter",
                "NETWORK": "Likely network-related",
                "UNDETERMINED": "Network/local undetermined",
            }.get(normalized, normalized)
        return {
            "LOCAL": "倾向本地卡顿",
            "NETWORK": "倾向网络相关",
            "UNDETERMINED": "本地/网络未确定",
        }.get(normalized, normalized)

    def _raw_metric_block(self, title: str, rows: list[str]) -> str:
        return (
            f'<div style="background:{BG2};border-radius:6px;padding:12px 14px;margin-top:8px;">'
            f'<div style="color:{TEXT};font-size:12px;font-weight:700;margin-bottom:8px;">{title}</div>'
            f'<table cellspacing="0" cellpadding="0" style="width:100%;">{"".join(rows)}</table>'
            f"</div>"
        )

    def _raw_metric_row(self, label: str, value: str) -> str:
        return (
            "<tr>"
            f'<td style="color:{MUTED};font-size:11px;padding:5px 16px 5px 0;white-space:nowrap;">{label}</td>'
            f'<td style="color:{TEXT};font-size:12px;padding:5px 0;">{value}</td>'
            "</tr>"
        )

    @staticmethod
    def _format_ratio(value: float) -> str:
        return f"{max(value, 0.0) * 100:.1f}%"

    @staticmethod
    def _escape(text: str) -> str:
        return (
            str(text or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
