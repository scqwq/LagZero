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

**持久化必须离开 UI 线程**（本轮修复）：`save_event`/`save_snapshot`/`event_count` 曾直接在 `_on_lag_ended`/`_on_frame_stutter_ended` 槽里执行，SQLAlchemy Session + 提交正好堵在用户切换报告时。现在写入经 `_persist_event_async` 丢给 daemon 线程，完成后 `event_persisted` 信号回 UI 线程补 id；`event_count` 同样异步（`event_count_loaded`）。Storage 引擎相应加固：WAL 模式（读写不再全库互斥）、`busy_timeout=10s`、NullPool（每 Session 独立连接，连接不跨线程复用）。实测 40 并发读写零错误。

**实时指标窗口是时间窗口不是帧数窗口**（本轮修复）：`_PresentMonParser._recent_frames` 曾是 `deque(maxlen=180)`——240fps 下 0.75 秒，但 20fps 下 9 秒，低帧率游戏的 FPS/均值/P95 面板滞后十几秒才反映变化。现在 `METRICS_WINDOW_S = 2.0` 秒时间窗口（deque maxlen=4096 兜底防失控），`_trim_recent_frames()` 按解析时刻墙钟裁剪，任何帧率下面板滞后 ≤ ~2.2 秒。

**逐帧数据不得回 UI 线程**（本轮修复）：`_PresentMonParser.sample_parsed` 曾额外连接到主线程 `PresentMonBridge._on_sample_parsed()`，而该槽只维护 `_received_frame` / `_last_failure_reason` 两个状态。240fps 下这会每秒向 GUI 事件队列投递 240 个跨线程事件，鼠标/重绘/报告切换被排队事件挤压，表现为界面偶发顿挫。现在这条连接已删除；接收状态在每 200ms 一次的 `metrics_ready` 回调中维护。`sample_parsed` 到 `FrameStutterDetector.ingest_frame()` 的逐帧链路保持不变，帧检测、基线学习和归因不受影响。

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
- 阈值 = `max(地板, mean×ratio, mean+margin, mean+Nσ)`，spike/stutter 各自的 ratio 默认 2.0/3.5、margin=14/28ms、σ=3/5。地板只是兜底不是决定项。
- **倍率可调**：`frame_spike_ratio` / `frame_stutter_ratio` 存在 `PressureSettings`（`data/pressure_settings.json`），设置页"卡顿判定灵敏度"提供两个 SpinBox（1.2–5.0× / 1.5–8.0×），修改即时生效，不重置基线。数值越小越敏感（报更多轻微波动），越大越保守（只报明显卡顿）。
- **预热期**（基线 <120 帧未就绪）退回旧的保守绝对值 50/66/150ms。曾经直接用地板 33ms 当预热阈值，30fps 游戏每帧都被判卡顿 → 卡顿帧不进基线 → 基线永远不就绪的死循环。
- **掉帧检测**：`was_displayed=False`（v2 `DisplayedTime==NA`）且近 12 帧内掉帧 ≥3 才触发 `FRAME_DROP`。这类卡顿帧时间完全正常，旧检测器彻底漏检；单帧掉帧在 240Hz 下只多 4ms 间隙、玩家无感，其可见后果由 DISPLAY_STALL 路径兜底。
- **上屏延迟**：displayed_time 超过自身基线 2 倍 + 超过帧成本 1.5 倍或 8ms 才算独立事件 —— displayed 跟着 frame_time 涨只是帧慢的结果，不算独立发现。
- **只学平静帧**：卡顿帧不进基线，否则连续卡顿的游戏会把自己的病态学成正常。
- 严重度按"峰值/基线倍率"而非绝对毫秒（240Hz 玩家 30ms=丢 7 帧，30fps 玩家 30ms=正常）。

**系统级（`detection.py`）**：CPU/RAM/响应性 sigmoid 评分 → 加权复合分（CPU 0.5 / RAM 0.2 / 响应 0.3）→ `0.6×加权+0.4×峰值维度` ≥ 0.45 且连续 2 个采样 → 卡顿。阈值是学到的 `mean+Nσ`（系统级基线 60 个采样就绪），**且带地板**：CPU ≥55%、RAM ≥70%、响应 ≥25ms。曾经无下限 —— 空闲机器学出 mean=8%σ=1 → 阈值 10%，游戏一开全判卡顿。响应性 σ 下限 2ms（低于 Windows 定时器量化噪声的 σ 只是把测量噪声学成了"精度"）。

**进程 CPU 口径（本轮修复的核心误报）**：psutil 的进程 `cpu_percent` 是单核归一化（100% = 1 个核）。所有进程级阈值必须经 `collectors.per_core_to_machine_share()` 转成整机占比后再比较：单进程规则 40% 整机、游戏本体豁免线 60% 整机、后台集群成员 5% 整机、兼容模式 CPU 压力 70% 整机。直接拿原始值比固定阈值会把 32 线程机器上 200%（=整机 6%）的游戏误报成"占满 CPU 导致卡顿"。

**兼容模式（`compat_detector.py`）**：窗口挂起 / 视觉冻结连击≥8 / 响应尖峰（120ms/250ms 两级）+ CPU/IO 压力（CPU 已是整机占比口径）。

## 归因算法（`frame_attribution.py`，本轮新增）

回答"哪一段出了问题"。核心是**超额时间分桶**：卡顿帧的各阶段耗时各自减去本会话基线（`WelfordBaseline`），把增量分进四个桶：
1. `CPU_BOUND` ← CPUBusy 超额（游戏自身 CPU 变重）
2. `GPU_BOUND` ← GPUBusy 超额 **+ CPUWait 中能被 GPU 增量解释的部分**（队列反压是 GPU 问题不是 CPU 问题）
3. `DRIVER_RENDER_PATH`（呈现路径阻塞）← CPUWait 超额减去 GPU 能解释的剩余
4. `DISPLAY_PIPELINE` ← displayed_time 超额中**超出帧成本增量的部分**（displayed 与 frame 同涨是同一件事，不能重复计账）+ 掉帧比例 × 基线帧时间

桶按大小排序，主导桶即 verdict；置信度 = 0.30 + 0.45×份额 + 0.25×与次名的差距，基线未就绪 ×0.75，总超额 <8ms 再 ×0.8。证据行只报份额 ≥12% 的桶。冒烟验证：GPU/CPU/呈现路径三类 240Hz 卡顿各自 99% 单一归因，互不串扰。

置信度 <0.5 的 verdict 只作弱提示（`WEAK_CATEGORIES` 机制），系统侧规则（具体进程、内存耗尽等）可以覆盖它；置信度 ≥0.5 时帧侧 verdict 直接成为事件 category。

### 上下文精炼（`analyzer.py`，本轮新增）

帧归因只看 PresentMon 的帧内阶段增量，不知道整机 CPU 是否吃满。"CPUBusy 桶最大"不能直接等同于"CPU 瓶颈"——切窗口、单线程瓶颈、真饱和都会让 CPUBusy 增长。`analyze_frame_episode` 在仲裁前调用 `_refine_frame_verdict(attribution, peak_sample)`，用系统快照把初步 verdict 精炼为最终 category：

| 条件 | 精炼结果 | 含义 |
|---|---|---|
| `CPU_BOUND` + `system_cpu >= 85%` | 保留 `CPU_BOUND` | 整机真饱和，游戏抢不到 CPU |
| `CPU_BOUND` + `target_cpu >= 20%` 或 `system_cpu >= 70%` | → `CPU_STAGE_STALL` | 游戏在用 CPU 但系统有余量，引擎内部瓶颈（单线程/锁/等待） |
| `CPU_BOUND` + 其余 | → `TRANSIENT_DISTURBANCE` | 整机和游戏 CPU 都低，可能是窗口切换/瞬时干扰 |
| `DRIVER_RENDER_PATH` + `wait_share >= 0.4` + 后台进程 `>= 10%` 整机 | → `SCHEDULER_CONTENTION` | CPU 等待增长 + 后台进程抢 CPU，调度抢占 |

新增分类 `CPU_STAGE_STALL` / `TRANSIENT_DISTURBANCE` 不在 `WEAK_CATEGORIES`，会赢过 UNDETERMINED，但具名系统发现（进程/RAM 等）仍然可以覆盖它们。证据行保持帧归因的原始阶段分解（描述事实），精炼后的 category 决定报告标题/颜色（做解释）。

## 卡顿报告生成思路

事件结束 → `MainWindow._on_frame_stutter_ended`：先 `recorder.capture(event)` 拿系统快照（空 pre_lag 缓冲 = 占位全零样本，必须传 `None` 给分析器，否则规则会从零里编造结论）→ `CauseAnalyzer.analyze_frame_episode(episode, peak_sample, pre_lag_samples)`：
- 系统规则给出具体 verdict（具名进程、内存等）时优先；落在弱类（UNDETERMINED/LOCAL_STUTTER）而帧归因有具体结论时，帧侧胜出
- 产出 `FrameCauseResult(category, explanation, scope, frame_summary, system_cause, used_system_cause)`，两个半边分开存：`cause`（为什么）+ `frame_summary`（玩家看到了什么）
- 中文报告在 `detail_panel._frame_summary_zh` 里**按标签正则抽取**数字（不是按位置索引——加一句话就全错位），`_has_system_context` 挡住占位快照的零值冒充实测
- 报告结构：标题行（图标+分类+时长+scope）→ 原因段 → 峰值指标四格 → 游戏原始指标 → 卡顿前 5s 时间线（sparkline）→ 峰值进程表。清晰但不冗余：证据行有 12% 份额门槛，输入延迟只在实际测到时出现

**新增分类必须同时进三个字典**（`detail_panel.py` 的 `CAUSE_LABELS_ZH` / `CAUSE_ICONS` / `CAUSE_COLOURS`），否则 `test_every_category_has_report_labels` 失败。本轮新增 `CPU_STAGE_STALL`、`TRANSIENT_DISTURBANCE` 已全部补齐。

**进程 CPU 报告口径（本轮修复）**：报告文案统一使用整机占比（`cpu_machine_share`），旧数据库无此字段时回退 `cpu_percent / machine_cpu_count()`。用户看到"整机 15.6%（约 500.0% 单核计）"而不是裸的 500%。`_rule_single_cpu_spike` 的英文 explanation 同样只用整机占比。

**卡顿报告与压力警告分离（`detection_source` 字段）**：事件列表顶部有"卡顿报告"/"压力警告"两个互斥筛选按钮，切换时清空当前行并从数据库按筛选条件重新加载。每个 `LagEvent` 携带 `detection_source` 字段标记来源：

| detection_source | 含义 | 归属标签页 |
|---|---|---|
| `frame` | PresentMon 帧级检测（帧时间/掉帧/上屏异常） | 卡顿报告 |
| `compat` | 兼容模式检测，有明确卡顿证据（窗口挂起/视觉冻结/响应尖峰） | 卡顿报告 |
| `compat_pressure` | 兼容模式检测，仅资源压力+中等响应延迟（CPU/IO ≥60ms） | 压力警告 |
| `system` | 系统级复合评分（当前已禁用；若启用自动归压力） | 压力警告 |
| `pressure` | 压力评估（无卡顿时的资源阈值超标） | 压力警告 |

存储层 `get_recent_events` / `event_count` / `delete_all_events` 按 `detection_source` 过滤：`"stutter"` 取 `frame+compat`，`"pressure"` 取 `pressure+system+compat_pressure`。旧数据库迁移时按 `category` 回填 `detection_source`（`RESOURCE_PRESSURE_RISK` → pressure，`CPU Pressure Stall` / `I/O Pressure Stall` → compat_pressure，其余 → frame）。`EventLogWidget._matches_filter` 做同样的分类；`upsert_event` 在事件分析完成后如果 `detection_source` 变化（如 compat → compat_pressure），会自动将事件从当前标签页移除。"清空全部"只删除当前筛选类型的事件。

**设置保存去抖**：`QDoubleSpinBox.valueChanged` 在每格箭头/滚轮/键入时都会触发，曾经每次都同步 `write_text` 到磁盘，低 RAM 机器上连续滚动会阻塞 UI。现在 `_settings_save_timer`（500ms 单次 QTimer）在停止调整后才写盘；内存中的 settings 和 `update_sensitivity` 仍然立即生效。"恢复默认阈值"按钮绕过去抖、立即持久化。

**按钮与 SpinBox 样式**：全局 `QPushButton` 增加 `:hover`（边框变蓝+背景提亮）和 `:pressed`（更亮背景+内缩 padding）状态；`QDoubleSpinBox` 显式绘制 `::up-button` / `::down-button`（24px 宽、CSS 三角箭头、hover/pressed 变色）。SpinBox 同时开启 `setAccelerated(True)`（长按加速）和 `setKeyboardTracking(False)`（键入中途不触发信号）。

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
