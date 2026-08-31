"""
ui/detail_panel.py — Right panel shown when a lag event is selected.

Rendered as a single rich-text view to keep report switching lightweight.
"""
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextBrowser

from core.models import LagEvent, LagSnapshot
from core.collectors import machine_cpu_count

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
    "CPU_STAGE_STALL": "🔧",
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
    "TRANSIENT_DISTURBANCE": "⚡",
    "DISPLAY_PIPELINE": "🖥️",
    "FRAME_PACING_COLLAPSE": "📉",
    "LOCAL_STUTTER": "📉",
    "UNDETERMINED": "❓",
    "FRAME_SPIKE": "📉",
    "FRAME_STUTTER": "🎞️",
    "FRAME_FREEZE": "🧊",
    "FRAME_DROP": "🕳️",
    "DISPLAY_STALL": "🖥️",
    "Window Not Responding": "🧊",
    "Visual Freeze": "🖼️",
    "Responsiveness Stall": "⏱️",
    "CPU Pressure Stall": "🔥",
    "I/O Pressure Stall": "💿",
    "RESOURCE_PRESSURE_RISK": "⚠️",
    "UNKNOWN": "❓",
}

CAUSE_COLOURS = {
    "REPORT_PENDING": ACCENT,
    "CPU_SPIKE": RED,
    "CPU_BOUND": RED,
    "CPU_STAGE_STALL": AMBER,
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
    "TRANSIENT_DISTURBANCE": MUTED,
    "DISPLAY_PIPELINE": PURPLE,
    "FRAME_PACING_COLLAPSE": "#e67e22",
    "LOCAL_STUTTER": AMBER,
    "UNDETERMINED": MUTED,
    "FRAME_SPIKE": ACCENT,
    "FRAME_STUTTER": "#e67e22",
    "FRAME_FREEZE": RED,
    "FRAME_DROP": PURPLE,
    "DISPLAY_STALL": PURPLE,
    "Window Not Responding": RED,
    "Visual Freeze": ACCENT,
    "Responsiveness Stall": AMBER,
    "CPU Pressure Stall": RED,
    "I/O Pressure Stall": "#1abc9c",
    "RESOURCE_PRESSURE_RISK": AMBER,
    "UNKNOWN": MUTED,
}

CAUSE_LABELS_ZH = {
    "REPORT_PENDING": "正在生成报告",
    "CPU_SPIKE": "单进程 CPU 峰值",
    "CPU_BOUND": "CPU 瓶颈",
    "CPU_STAGE_STALL": "游戏 CPU 阶段受限",
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
    "TRANSIENT_DISTURBANCE": "瞬时干扰",
    "DISPLAY_PIPELINE": "画面上屏异常",
    "FRAME_PACING_COLLAPSE": "短时连续掉速",
    "LOCAL_STUTTER": "本地卡顿",
    "UNDETERMINED": "未确定类型",
    "FRAME_SPIKE": "帧时间尖峰",
    "FRAME_STUTTER": "帧时间卡顿",
    "FRAME_FREEZE": "画面冻结",
    "FRAME_DROP": "帧未上屏",
    "DISPLAY_STALL": "上屏间隔过长",
    "Window Not Responding": "窗口未响应",
    "Visual Freeze": "画面冻结",
    "Responsiveness Stall": "响应延迟卡顿",
    "CPU Pressure Stall": "CPU 压力卡顿",
    "I/O Pressure Stall": "IO 压力卡顿",
    "RESOURCE_PRESSURE_RISK": "资源压力风险",
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
        det_code = event.cause_code or event.category or "UNKNOWN"
        attr_code = event.category or ""
        code = attr_code or det_code
        colour = CAUSE_COLOURS.get(code, MUTED)
        icon = CAUSE_ICONS.get(code, "❓")
        title = self._cause_label(det_code)
        attr_label = self._cause_label(attr_code) if attr_code and attr_code != det_code else ""
        scope = self._scope_label(event.scope)
        if event.is_pending:
            duration = "Generating..." if self._report_language == "en" else "正在生成报告"
        else:
            duration = f"{round(event.duration_seconds, 1)}s"

        title_line = f'<div style="color:{colour};font-size:15px;font-weight:700;">{icon} {title}'
        if attr_label:
            title_line += f' <span style="color:{MUTED};font-size:12px;font-weight:400;">· {attr_label}</span>'
        title_line += '</div>'

        html = [
            f'<div style="background:{BG2};border-left:3px solid {colour};border-radius:6px;padding:14px 16px;">',
            title_line,
            f'<div style="color:{MUTED};font-size:12px;margin-top:6px;">{event.started_at.strftime("%A, %b %d at %H:%M:%S")} · {duration} · {scope}</div>',
            f'<div style="color:{TEXT};font-size:13px;margin-top:10px;line-height:1.45;">{self._escape(self._explanation_text(event, snapshot))}</div>',
            "</div>",
        ]

        # A snapshot with no samples is the recorder's zero-filled placeholder, so
        # showing "Peak Metrics" for it would present 0% CPU / 0 ms response as
        # measured readings for an event that clearly had a problem.
        if snapshot is not None and self._has_system_context(snapshot):
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
            if snapshot.process_groups:
                html.append(self._section_html("Browser Groups At Peak" if self._report_language == "en" else "峰值时刻浏览器进程"))
                html.append(self._process_groups_html(snapshot))

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
        has_system_memory = snapshot.peak_sample.ram_total_mb > 0
        if target is None and gpu is None and not has_system_memory:
            return ""

        sections: list[str] = []
        if has_system_memory:
            title = "System Memory" if self._report_language == "en" else "系统内存"
            rows = [
                self._raw_metric_row("RAM Usage" if self._report_language == "en" else "内存使用率", f"{snapshot.peak_sample.ram_percent:.1f}%"),
                self._raw_metric_row("Used" if self._report_language == "en" else "已用内存", f"{snapshot.peak_sample.ram_used_mb / 1024:.2f} GB"),
                self._raw_metric_row("Available" if self._report_language == "en" else "可用内存", f"{snapshot.peak_sample.ram_available_mb / 1024:.2f} GB"),
                self._raw_metric_row("Total" if self._report_language == "en" else "总内存", f"{snapshot.peak_sample.ram_total_mb / 1024:.2f} GB"),
            ]
            sections.append(self._raw_metric_block(title, rows))

        if target is not None:
            title = "Target Process" if self._report_language == "en" else "目标进程"
            rows = [
                self._raw_metric_row("Name" if self._report_language == "en" else "名称", self._escape(target.name)),
                self._raw_metric_row("PID", str(target.pid)),
                self._raw_metric_row(
                    "CPU Share" if self._report_language == "en" else "CPU（整机）",
                    f"{target.cpu_machine_share:.1f}%",
                ),
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
        headers = (
            ("PROCESS", "PID", "CPU SHARE", "RAM")
            if self._report_language == "en"
            else ("进程", "PID", "CPU（整机）", "内存")
        )
        rows = [
            f"<tr>"
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[0]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[1]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[2]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 0 8px 0;">{headers[3]}</td>'
            f"</tr>"
        ]
        for proc in snapshot.top_processes[:32]:
            cpu_share = proc.cpu_machine_share
            cpu_colour = RED if cpu_share > 20 else AMBER if cpu_share > 10 else TEXT
            rows.append(
                f"<tr>"
                f'<td style="color:{TEXT};font-size:12px;padding:4px 12px 4px 0;">{self._escape(proc.name[:28])}</td>'
                f'<td style="color:{MUTED};font-size:12px;padding:4px 12px 4px 0;">{proc.pid}</td>'
                f'<td style="color:{cpu_colour};font-size:12px;font-weight:700;padding:4px 12px 4px 0;">{cpu_share:.1f}%</td>'
                f'<td style="color:{MUTED};font-size:12px;padding:4px 0;">{proc.memory_mb:.0f} MB</td>'
                f"</tr>"
            )
        return f'<div style="background:{BG2};border-radius:6px;padding:12px 14px;margin-top:8px;"><table cellspacing="0" cellpadding="0">{"".join(rows)}</table></div>'

    def _process_groups_html(self, snapshot: LagSnapshot) -> str:
        headers = (
            ("BROWSER", "PROCESSES", "CPU SHARE", "RAM")
            if self._report_language == "en"
            else ("浏览器", "进程数", "CPU（整机）", "内存（私有）")
        )
        rows = [
            "<tr>"
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[0]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[1]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 12px 8px 0;">{headers[2]}</td>'
            f'<td style="color:{MUTED};font-size:10px;font-weight:700;padding:0 0 8px 0;">{headers[3]}</td>'
            "</tr>"
        ]
        for group in snapshot.process_groups[:6]:
            cpu_colour = RED if group.cpu_machine_share > 20 else AMBER if group.cpu_machine_share > 10 else TEXT
            rows.append(
                "<tr>"
                f'<td style="color:{TEXT};font-size:12px;padding:4px 12px 4px 0;">{self._escape(group.name[:28])}</td>'
                f'<td style="color:{MUTED};font-size:12px;padding:4px 12px 4px 0;">{group.process_count}</td>'
                f'<td style="color:{cpu_colour};font-size:12px;font-weight:700;padding:4px 12px 4px 0;">{group.cpu_machine_share:.1f}%</td>'
                f'<td style="color:{MUTED};font-size:12px;padding:4px 0;">{group.memory_mb:.0f} MB</td>'
                "</tr>"
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
        """
        Cause text for the report header.

        The Chinese branch rewrites the cause per category rather than using
        event.cause verbatim, so the frame timings have to be re-attached
        explicitly — otherwise routing frame events through the cause analyzer
        would trade away the "what the player saw" half of the report.
        """
        if self._report_language == "en":
            if event.is_pending:
                return "This stutter is still in progress. The final report will be filled in after the game recovers."
            cause = event.cause or "No explanation available."
            summary = (event.frame_summary or "").strip()
            # The analyzer already folds the summary into `cause` when the system
            # rules won, so append only when it is genuinely missing instead of
            # printing the same timings twice.
            if summary and summary not in cause:
                return f"{cause}\n\n{summary}"
            return cause
        code = event.category or event.cause_code or "UNKNOWN"
        if event.is_pending:
            return "这次卡顿仍在进行中。为了避免正在生成报告本身拖慢界面，当前先显示轻量占位，等恢复后再补全完整结论。"
        if snapshot is None or not self._has_system_context(snapshot):
            # Without real samples the per-category text below would quote the
            # recorder's zero-filled placeholder as if it had been measured
            # ("峰值 CPU 0%"). Say what is actually known instead.
            return self._with_frame_summary(
                event, "本次卡顿没有采集到配套的系统快照，因此只能给出玩家侧的表现，暂时无法定位系统层根因。"
            )
        return self._with_frame_summary(event, self._cause_text_zh(event, snapshot, code))

    @staticmethod
    def _has_system_context(snapshot: LagSnapshot) -> bool:
        """
        True when the snapshot holds real measurements.

        A frame event can end before the sampler ever produced a sample, and both
        the recorder and the storage reader synthesise a zero-filled SystemSample
        in that case. Rendering it would invent numbers, so callers gate on this.
        """
        return bool(snapshot.pre_lag_samples or snapshot.top_processes)

    def _with_frame_summary(self, event: LagEvent, text: str) -> str:
        summary = self._frame_summary_zh(event)
        if not summary:
            return text
        return f"{text}\n\n{summary}"

    @staticmethod
    def _frame_summary_zh(event: LagEvent) -> str:
        """
        Translate the analyzer's frame summary into Chinese.

        The analyzer emits it in English so it can be stored and reused
        language-neutrally; parsing it back would be fragile, so the numbers are
        re-extracted by regex and only the recognised shape is rendered.
        """
        raw = (event.frame_summary or "").strip()
        if not raw:
            return ""
        is_compat = raw.startswith("Peak response delay")
        label = "峰值响应延迟" if is_compat else "峰值帧时间"
        # Matched by label, not by position. The previous version collected every
        # "N ms" in the string and indexed into the list, so adding one sentence
        # to the summary silently relabelled the numbers after it — the session
        # baseline would have been printed as "peak CPU wait".
        head = re.search(
            r"reached ([\d.]+) ms \(avg ([\d.]+) ms, P95 ([\d.]+) ms\)", raw
        )
        if not head:
            return ""
        parts = [
            f"玩家侧表现：{label} {head.group(1)} ms"
            f"（平均 {head.group(2)} ms，P95 {head.group(3)} ms）。"
        ]
        baseline = re.search(r"Normal for this session was ([\d.]+) ms", raw)
        if baseline:
            parts.append(f"该会话正常帧时间约 {baseline.group(1)} ms。")
        freeze = re.search(r"(\d+) freeze-level samples", raw)
        slow = re.search(r"(\d+) slow samples", raw)
        if freeze:
            parts.append(f"其中 {freeze.group(1)} 个采样达到冻结级别。")
        elif slow:
            parts.append(f"其中 {slow.group(1)} 个采样偏慢。")
        dropped = re.search(r"(\d+) frames never reached the screen", raw)
        if dropped:
            parts.append(f"另有 {dropped.group(1)} 帧没有出现在屏幕上。")
        stages = re.search(
            r"Peak CPU wait ([\d.]+) ms, peak GPU busy ([\d.]+) ms", raw
        )
        if stages:
            parts.append(
                f"峰值 CPU 等待 {stages.group(1)} ms，峰值 GPU 忙 {stages.group(2)} ms。"
            )
        latency = re.search(r"Peak end-to-end input latency ([\d.]+) ms", raw)
        if latency:
            parts.append(f"端到端输入延迟峰值 {latency.group(1)} ms。")
        return " ".join(parts)

    def _cause_text_zh(self, event: LagEvent, snapshot: LagSnapshot, code: str) -> str:
        top_proc = snapshot.top_processes[0] if snapshot.top_processes else None
        is_minor = (event.detection_source or "").strip() == "minor"
        if code == "RESOURCE_PRESSURE_RISK":
            if event.cause:
                return event.cause
            available_gb = snapshot.peak_sample.ram_available_mb / 1024.0
            available_text = f"可用内存约 {available_gb:.2f} GB。" if available_gb > 0 else ""
            return (
                "当前尚未检测到明显帧卡顿，但系统资源压力较高，可能影响游戏流畅性。"
                f"峰值整机 CPU {snapshot.peak_cpu:.1f}%，内存占用 {snapshot.peak_ram:.1f}%。{available_text}"
            )
        if code in {"CPU_SPIKE", "CPU_BOUND"} and top_proc is not None:
            share = top_proc.cpu_machine_share
            if share <= 0.0:
                # Old snapshots may not have machine_share; compute it here.
                share = top_proc.cpu_percent / machine_cpu_count()
            return self._report_brief(
                "更像 CPU 资源已经被明显吃满，游戏抢不到足够的计算时间。",
                f"峰值时 {top_proc.name} (PID {top_proc.pid}) 占到整机 {share:.1f}% CPU，属于可以直接拖慢系统的级别。",
                "先关闭或限制这个高占用进程，再观察卡顿是否明显减少；若它就是游戏本体，可优先降低偏 CPU 的画面/物理选项或限制帧率。",
            )
        if code == "CPU_STAGE_STALL":
            target = snapshot.peak_sample.target_process
            if target is not None and target.cpu_machine_share > 0:
                return self._report_brief(
                    "更像游戏自己的 CPU 阶段突然变慢，而不是整机 CPU 完全不够。",
                    f"游戏约占整机 {target.cpu_machine_share:.1f}% CPU，但整机没有明显吃满，范围已缩到主线程、锁竞争或引擎内部等待这一侧。",
                    "优先降低偏 CPU 的设置，关闭后台干扰，并观察是否总在同类场景反复出现；若总在固定场景触发，更像游戏内容或引擎侧卡点。",
                )
            return self._report_brief(
                "更像游戏自己的 CPU 阶段突然变慢，而不是整机 CPU 完全不够。",
                "整机 CPU 没有明显吃满，因此更接近主线程、锁竞争或引擎内部等待。",
                "优先降低偏 CPU 的设置，关闭后台干扰，并观察是否总在同类场景反复出现。",
            )
        if code == "TRANSIENT_DISTURBANCE":
            return self._report_brief(
                "更像一次瞬时干扰，不像持续的 CPU、GPU 或内存瓶颈。",
                "帧变慢了，但整机 CPU 和游戏自身 CPU 都不高，范围更接近切窗口、焦点变化或短时系统打扰。",
                "如果只是偶发，可先继续观察；若常伴随切屏、弹窗、录屏或悬浮层出现，优先从这些干扰源排查。",
            )
        if code == "GPU_BOUND":
            return self._report_brief(
                "更像 GPU 渲染这段压力过高，显卡侧成为主要瓶颈。",
                "本次额外耗时主要堆在 GPU 渲染阶段，不像单纯 CPU 或内存不足。",
                "优先降低分辨率、阴影、特效、抗锯齿与体积效果；若只在烟雾、爆炸、复杂场景出现，基本就是显卡场景压力。",
            )
        if code == "VRAM_PRESSURE":
            gpu = snapshot.peak_sample.gpu_memory
            detail = ""
            if gpu is not None and gpu.local_budget_mb > 0:
                detail = f"本地显存预算占比约 {gpu.local_usage_ratio * 100:.0f}%。"
            return self._report_brief(
                "更像显存余量不足，而不只是纯 GPU 算力不够。",
                f"显存预算已经很高。{detail}".strip(),
                "优先降低贴图、分辨率、材质缓存和高显存占用特效；多开录屏、浏览器硬件加速或多屏环境也可能放大这类问题。",
            )
        if code in {"RAM_EXHAUSTION", "RAM_PRESSURE", "SYSTEM_RAM_PRESSURE"}:
            return self._report_brief(
                "更像系统内存余量不足，游戏开始受到内存压力影响。",
                f"峰值系统内存占用约 {snapshot.peak_ram:.0f}%，这类卡顿常伴随加载变慢、分页和帧时间抖动。",
                "优先关闭浏览器、启动器、录屏和大内存后台程序；如果经常发生，可考虑降低游戏内存占用或增加物理内存。",
            )
        if code == "GAME_MEMORY_LIMIT":
            target = snapshot.peak_sample.target_process
            if target is not None:
                return self._report_brief(
                    "更像游戏进程自己的可用内存上限偏紧，而不是整机 RAM 完全不够。",
                    f"{target.name} 的内存长期贴近较窄上限，但系统总内存并未同时吃满。",
                    "优先检查启动参数、MOD、材质包和游戏自身内存设置；若只在长时间游玩后出现，也可怀疑游戏侧内存管理问题。",
                )
            return self._report_brief(
                "更像游戏进程自己的可用内存上限偏紧，而不是整机 RAM 完全不够。",
                "目标进程像是被限制在较窄的内存使用上限内。",
                "优先检查启动参数、MOD、材质包和游戏自身内存设置。",
            )
        if code in {"BACKGROUND_CLUSTER", "BACKGROUND_INTERFERENCE"}:
            return self._report_brief(
                "更像多个后台程序叠加干扰，而不是单一硬件瓶颈。",
                "没有唯一元凶，但多个后台进程同时占资源，容易一起抢走游戏需要的 CPU 时间片。",
                "优先关闭浏览器、多标签页、下载器、录屏和扫描任务；若下一次峰值进程名单里总是同几类程序，先从它们下手。",
            )
        if code == "DISPLAY_PIPELINE":
            return self._report_brief(
                "更像帧已经做出来了，但显示到屏幕这最后一步出现了延迟或抖动。",
                "这次范围已经缩到上屏链路，而不是纯 CPU/GPU 算力不足；常见于切窗口、桌面合成、覆盖层、帧生成或显示链路短时波动。",
                "如果只是偶发，先从切窗口和覆盖层排查；若反复出现，再检查全屏模式、驱动、同步设置和多屏/录屏环境。",
            )
        if code in {"DISK_IO", "IO_STALL"}:
            return self._report_brief(
                "更像磁盘或 IO 路径在拖慢系统，而不是单纯 CPU 或 GPU 不够。",
                f"峰值系统响应延迟达到 {snapshot.peak_responsiveness_ms:.1f} ms，这类情况常见于加载、解压、杀毒扫描或分页。",
                "优先暂停下载、解压、扫描和同步盘；若同时伴随内存紧张，也要把内存压力一起看。",
            )
        if code == "DRIVER_RENDER_PATH":
            return self._report_brief(
                "更像驱动、Present 提交或渲染链路被卡住，不像典型的 CPU、RAM 或磁盘瓶颈。",
                "范围已缩到驱动/呈现路径这一段，但还不能把原因唯一锁死；覆盖层、录屏、桌面合成和驱动异常都可能落在这里。",
                "优先关闭覆盖层、录屏和悬浮监控，再观察；若持续复现，可尝试更新或回退显卡驱动，并切换全屏/无边框模式对比。",
            )
        if code in {"SCHEDULER_CONTENTION", "LOCAL_STUTTER"}:
            if code == "SCHEDULER_CONTENTION":
                return self._report_brief(
                    "更像游戏线程在抢 CPU 时间片时被后台程序打断了。",
                    "CPU 等待上升，同时后台进程占用明显，范围已缩到调度抢占而不是单纯 GPU 或显存问题。",
                    "先关闭高 CPU 后台程序、浏览器、录屏和下载器，再复测；如果改善明显，基本可以确认是资源争抢。",
                )
            return self._report_brief(
                "确认发生了本地卡顿，但暂时还没有足够强的证据把原因唯一锁定。",
                f"峰值 CPU {snapshot.peak_cpu:.0f}%，响应延迟 {snapshot.peak_responsiveness_ms:.1f} ms；当前更像综合性压力或短时场景波动。",
                "先观察它是否总和某一类场景、后台程序或资源压力一起出现；如果后续重复集中到同一类别，再优先按那条线排查。",
            )
        if code == "FRAME_PACING_COLLAPSE":
            fps_ratio = 100.0
            summary = ""
            raw = (event.frame_summary or "").strip()
            match = re.search(r"about ([\d.]+)% of normal", raw)
            if match:
                fps_ratio = float(match.group(1))
                summary = f"短时间内有效帧率掉到正常的大约 {fps_ratio:.0f}%。"
            return self._report_brief(
                "更像不是一帧特别长，而是一小段时间内连续很多帧一起变差。",
                summary or "这类卡顿常见于短时资源加载、后台争抢或场景复杂度瞬时上升。",
                "优先结合当时场景看是否伴随转点、爆炸、烟雾、切界面或后台活动；若总在同类时机出现，就按那条线继续排查。",
            )
        if code in {"FRAME_SPIKE", "FRAME_STUTTER", "FRAME_FREEZE", "FRAME_DROP", "DISPLAY_STALL"}:
            if is_minor:
                return self._report_brief(
                    "确认出现了轻度帧级波动或短时掉速。",
                    f"这次现象持续约 {event.duration_seconds:.1f} 秒，更接近玩家看到的结果；当前还不能唯一锁定硬件根因。",
                    "若后续总伴随同一种归因或同类场景重复出现，再优先从那一侧排查。",
                )
            return self._report_brief(
                "确认出现了明显的帧级卡顿或掉帧现象。",
                f"这次报告更接近玩家看到的结果，持续约 {event.duration_seconds:.1f} 秒；若没有更具体归因，说明当前还不能唯一锁定硬件根因。",
                "可结合同一时间的 CPU、RAM、显存和峰值进程继续判断；如果后续总伴随同一种归因，再优先从那一侧排查。",
            )
        if code == "Window Not Responding":
            return self._report_brief(
                "更像游戏窗口主线程被卡住，连窗口消息都没能及时处理。",
                "这是比较重的本地异常，常见于主线程阻塞、极重加载或驱动等待。",
                "优先排查后台干扰、磁盘/内存压力和驱动问题；若频繁出现，建议重点看游戏本体或 MOD。",
            )
        if code == "Visual Freeze":
            return self._report_brief(
                "检测到画面长时间不变化，属于视觉冻结级别异常。",
                "范围更接近渲染线程停顿、资源加载阻塞或驱动层等待。",
                "优先观察是否总在加载、切场景或复杂特效时出现，并同步排查驱动、存储和内存压力。",
            )
        return event.cause or "暂无详细说明。"

    @staticmethod
    def _report_brief(primary: str, context: str, action: str) -> str:
        parts = [f"主判断：{primary}"]
        context_text = (context or "").strip()
        action_text = (action or "").strip()
        if context_text:
            parts.append(f"伴随线索：{context_text}")
        if action_text:
            parts.append(f"优先建议：{action_text}")
        return "\n".join(parts)

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
