# LagLense

Windows 游戏卡顿检测工具（PySide6 / Qt），驱动 Intel PresentMon 2.5.1 作为外部子进程采集帧级数据。本文件面向后续开发窗口，说明项目现状、架构与本轮改动。

## 运行与测试

```bash
# 运行（需要管理员权限；程序会经 core/elevation.py 自行 UAC 提权重启）
python main.py

# 测试（pytest 未安装；控制台是 GBK，必须带编码前缀）
PYTHONIOENCODING=utf-8 python tests/test_detection.py
```

- PresentMon 二进制实际文件名是 `tools/PresentMon/PresentMon-2.5.1-x64.exe`（`DEFAULT_PRESENTMON_PATH` 常量里的 `PresentMon.exe` 不存在，resolve 逻辑会找真实文件）。
- PresentMon 输出是带 BOM 的 UTF-16LE，即使被管道重定向也是（`_StreamTextDecoder` 做有状态解码，处理 chunk 边界切开代理对的情况）。
- 打包用 PyInstaller（`LagLens.spec`，已 gitignore，含 `uac_admin=True`）。

## 架构总览

```
main.py                 装配线程图（见下）
core/
  elevation.py          UAC 自提权（ShellExecuteW "runas" 重启自身）
  presentmon_bridge.py  PresentMon 子进程管理 + CSV 解析（QProcess，独立线程）
  frame_detector.py     帧级卡顿检测器（基线自适应，独立 QThread）
  frame_attribution.py  卡顿归因算法（本轮新增）
  baseline.py           WelfordBaseline 流式均值/方差（本轮新增）
  compat_capture.py     兼容模式采集（psutil + 窗口截图哈希，400ms）
  compat_detector.py    兼容模式卡顿检测器
  collectors.py         系统采样线程（CPU/RAM/进程/响应性，1s）
  detection.py          系统级 DetectionEngine（复合评分）
  analyzer.py           CauseAnalyzer 规则引擎（8 条规则，首个命中生效）
  storage.py            SQLAlchemy + SQLite，手动 PRAGMA/ALTER TABLE 迁移
  game_session.py       游戏窗口候选发现（EnumWindows）
  gpu_stats.py          显存查询
ui/
  main_window.py        主窗口（~1350 行）
  detail_panel.py       右侧报告面板（富文本）
  event_log.py          事件列表
  tray.py, theme.py 等
```

线程图（`main.py`）：解析器与两个检测器各占一个 `QThread`，`SystemCollector` 自身是 `QThread`，UI 只通过信号收结果 —— 这是"不让界面卡顿"的主要手段。UI 侧再做节流（`_metrics_timer` 250ms、`_set_capture_diag_text` 最小间隔 0.35–0.75s、状态栏去重）。

## 探测数据

**高精度模式（PresentMon `--v2_metrics`，本轮已切换）**，23 列，解析后进 `FrameSample`（`core/models.py`）：
- `FrameTime` → `frame_time_ms`；`CPUBusy`/`CPUWait` → `cpu_busy_ms`/`cpu_wait_ms`（CPU 干活 vs 被阻塞）
- `GPUBusy`/`GPUWait`/`GPUTime`/`GPULatency` → 同名字段
- `DisplayedTime` → `displayed_time_ms`，**其 `NA` 即"该帧从未上屏"** → `was_displayed=False`（这就是 v1 `Dropped` 的 v2 等价物；实测 median 与 FrameTime 完全一致，证明它就是显示间隔）
- `AnimationError` → `animation_error_ms`（`NA` → `has_animation_error=False`）
- `MsFlipDelay`/`AllInputToPhotonLatency`/`ClickToPhotonLatency` → `input_latency_ms`/`click_latency_ms`/`flip_delay_ms`。**实测桌面合成下 1837/1837 行全 NA**，所以输入延迟必须按"可用才报"处理，0.0 绝不能打印成"零延迟"
- `PresentMode`/`SyncInterval`/`AllowsTearing`/`PresentRuntime` 等元数据

v1 schema 仍可解析（自动按表头识别），字段映射已修正（旧映射把 busy/wait 弄反了）。`NA` 一律走 `_to_optional_float` → `None`，不折叠成 0.0 —— 否则掉帧会被伪装成"上屏 0 ms"。

**系统采样（1s，psutil）**：CPU 总量/每核、RAM/swap、响应性（1ms sleep 的实际耗时中位数）、top 进程（CPU 排序）、目标进程细项（私有内存、读写 KB/s、线程数）、显存预算（`gpu_stats`，2s 间隔，性能计数器）。

**兼容模式（400ms，psutil + GDI 截图哈希）**：窗口响应时间、`is_hung`（SendMessageTimeout）、视觉冻结连击、进程 CPU/内存/IO/线程。无帧级数据。

## 高精度 ↔ 兼容模式切换

- **进入兼容**：PresentMon 报错且消息含 `1450`（ETW 会话耗尽）/ `access denied` / `capture failed`，或 3 秒探针发现无 present 事件（`_on_capture_error` / `_on_presentmon_status`）。Java 进程直接视为不支持高精度。
- **自动回升**：兼容模式中每 20s 尝试一次高精度恢复探针（30s 冷却；`access denied`/`1450` 时不重试避免会话扰动）；成功后连续收到 6 个 `FrameMetricsSnapshot`（`COMPAT_RECOVERY_METRICS_REQUIRED`）即切回高精度。
- ETW 会话名固定 `LagLense-<pid>`，`--stop_existing_session` 只匹配同名会话 —— 会话名里带重试后缀曾导致孤儿会话堆积直到 1450。

## 卡顿检测

**帧级（`frame_detector.py`，本轮重写）**：每帧对照学到的基线判定，四级事件 `FRAME_SPIKE < FRAME_STUTTER < FRAME_DROP < FRAME_FREEZE`（`DISPLAY_STALL` 优先级 2）。
- 阈值 = `max(地板, mean×ratio, mean+margin, mean+Nσ)`，spike/stutter 各自的 ratio=2.0/3.5、margin=14/28ms、σ=3/5。地板只是兜底不是决定项。
- **预热期**（基线 <120 帧未就绪）退回旧的保守绝对值 50/66/150ms。曾经直接用地板 33ms 当预热阈值，30fps 游戏每帧都被判卡顿 → 卡顿帧不进基线 → 基线永远不就绪的死循环。
- **掉帧检测**：`was_displayed=False`（v2 `DisplayedTime==NA`）→ `FRAME_DROP`。这类卡顿帧时间完全正常，旧检测器彻底漏检。
- **上屏延迟**：displayed_time 超过自身基线 2 倍 + 超过帧成本 1.5 倍或 8ms 才算独立事件 —— displayed 跟着 frame_time 涨只是帧慢的结果，不算独立发现。
- **只学平静帧**：卡顿帧不进基线，否则连续卡顿的游戏会把自己的病态学成正常。
- 严重度按"峰值/基线倍率"而非绝对毫秒（240Hz 玩家 30ms=丢 7 帧，30fps 玩家 30ms=正常）。

**系统级（`detection.py`）**：CPU/RAM/响应性 sigmoid 评分 → 加权复合分（CPU 0.5 / RAM 0.2 / 响应 0.3）→ `0.6×加权+0.4×峰值维度` ≥ 0.45 且连续 2 个采样 → 卡顿。阈值是学到的 `mean+Nσ`（系统级基线 60 个采样就绪）。

**兼容模式（`compat_detector.py`）**：窗口挂起 / 视觉冻结连击≥8 / 响应尖峰（120ms/250ms 两级）+ CPU/IO 压力。

## 归因算法（`frame_attribution.py`，本轮新增）

回答"哪一段出了问题"。核心是**超额时间分桶**：卡顿帧的各阶段耗时各自减去本会话基线（`WelfordBaseline`），把增量分进四个桶：
1. `CPU_BOUND` ← CPUBusy 超额（游戏自身 CPU 变重）
2. `GPU_BOUND` ← GPUBusy 超额 **+ CPUWait 中能被 GPU 增量解释的部分**（队列反压是 GPU 问题不是 CPU 问题）
3. `DRIVER_RENDER_PATH`（呈现路径阻塞）← CPUWait 超额减去 GPU 能解释的剩余
4. `DISPLAY_PIPELINE` ← displayed_time 超额中**超出帧成本增量的部分**（displayed 与 frame 同涨是同一件事，不能重复计账）+ 掉帧比例 × 基线帧时间

桶按大小排序，主导桶即 verdict；置信度 = 0.30 + 0.45×份额 + 0.25×与次名的差距，基线未就绪 ×0.75，总超额 <8ms 再 ×0.8。证据行只报份额 ≥12% 的桶。冒烟验证：GPU/CPU/呈现路径三类 240Hz 卡顿各自 99% 单一归因，互不串扰。

置信度 <0.5 的 verdict 只作弱提示（`WEAK_CATEGORIES` 机制），系统侧规则（具体进程、内存耗尽等）可以覆盖它；置信度 ≥0.5 时帧侧 verdict 直接成为事件 category。

## 卡顿报告生成思路

事件结束 → `MainWindow._on_frame_stutter_ended`：先 `recorder.capture(event)` 拿系统快照（空 pre_lag 缓冲 = 占位全零样本，必须传 `None` 给分析器，否则规则会从零里编造结论）→ `CauseAnalyzer.analyze_frame_episode(episode, peak_sample, pre_lag_samples)`：
- 系统规则给出具体 verdict（具名进程、内存等）时优先；落在弱类（UNDETERMINED/LOCAL_STUTTER）而帧归因有具体结论时，帧侧胜出
- 产出 `FrameCauseResult(category, explanation, scope, frame_summary, system_cause, used_system_cause)`，两个半边分开存：`cause`（为什么）+ `frame_summary`（玩家看到了什么）
- 中文报告在 `detail_panel._frame_summary_zh` 里**按标签正则抽取**数字（不是按位置索引——加一句话就全错位），`_has_system_context` 挡住占位快照的零值冒充实测
- 报告结构：标题行（图标+分类+时长+scope）→ 原因段 → 峰值指标四格 → 游戏原始指标 → 卡顿前 5s 时间线（sparkline）→ 峰值进程表。清晰但不冗余：证据行有 12% 份额门槛，输入延迟只在实际测到时出现

**新增分类必须同时进三个字典**（`detail_panel.py` 的 `CAUSE_LABELS_ZH` / `CAUSE_ICONS` / `CAUSE_COLOURS`），否则 `test_every_category_has_report_labels` 失败。本轮新增 `DISPLAY_PIPELINE`、`FRAME_DROP`、`DISPLAY_STALL` 已全部补齐。

## PID 0 修复（用户指出）

`System Idle Process`（PID 0）计的是 CPU **空转**，空闲时 psutil 报 ~100%。旧代码不过滤，空闲机器会被报告成"System Idle Process 占用 95% CPU 导致卡顿"。现在：
- `collectors.py`：`is_idle_pseudo_process()`（PID+名字双匹配，名字在非英文系统会本地化）在采集时就过滤
- `analyzer.py`：`_rule_single_cpu_spike` / `_rule_background_cluster` 再过滤一次 —— SQLite 里的旧快照仍含 PID 0，规则不能信存量数据

## 资源与反作弊约束

- 解析/检测全在独立 QThread，UI 只收信号；文本更新有节流
- PresentMon 用 ETW（只读，不注入、不读写游戏内存），对 VAC 等反作弊是常规系统监控面；LagLense 自身只读 psutil/性能计数器。探针失败信息里保留 stderr 帮助诊断
- ETW 会话是系统级稀缺资源（64 个上限）：会话名稳定 + 启动前清理陈旧会话，避免把 1450 搞成常态

## 已知遗留

- `analyzer.py` 的 `CATEGORY_GPU_BOUND`/`CATEGORY_RAM_PRESSURE`/`SCOPE_NETWORK` 无规则产出（死常量，但 label 字典里有条目，测试靠它们通过）
- PresentMon 退出时 exit-code-127 噪音（git HEAD 上就有，与改动无关）
- `core/baseline.py` 顶部 `dataclass` import 未使用
- 系统级 `DetectionEngine._update_baseline` 仍是列表+statistics（未换成 `WelfordBaseline`，帧检测器用的是新模块；两处可统一）
