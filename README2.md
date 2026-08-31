# LagZero 维护说明

这份文档面向后续维护者，用来快速定位“默认值在哪算”“指数退却在哪接”“三类报告从哪生成”“两种模式都采了什么”。

## 1. 各个参数默认值的位置

### 默认值入口

- 主入口在 `core/pressure.py` 的 `default_settings(logical_cpu_count, total_ram_gb)`。
- 运行时加载入口在 `core/pressure.py` 的 `load_settings(...)`。
- 持久化位置是 `data/pressure_settings.json`，保存接口在 `core/pressure.py` 的 `save_settings(...)`。
- UI 初始化时由 `ui/main_window.py` 里的 `load_settings(...)` 载入，并映射到设置页 SpinBox。

### 各项默认值怎么来

- `frame_spike_ratio = 2.0`：`core/pressure.py`，单帧尖峰相对基线倍率默认值。
- `frame_stutter_ratio = 3.5`：`core/pressure.py`，明显帧卡顿相对基线倍率默认值。
- `obvious_stutter_sensitivity = 1.0`：`core/pressure.py`，明显卡顿灵敏度默认值。
- `continuous_wave_sensitivity = 1.0`：`core/pressure.py`，连续波动灵敏度默认值。
- `system_cpu_percent`：由 `background_total_cpu_threshold_percent(logical_cpu_count)` 计算，位置在 `core/pressure.py`。
- `ram_available_warning_gb`：由 `ram_available_warning_gb(total_ram_gb)` 计算，位置在 `core/pressure.py`。
- `background_process_cpu_percent`：由 `background_process_cpu_threshold_percent(logical_cpu_count)` 计算，位置在 `core/pressure.py`。
- `background_total_cpu_percent`：由 `background_total_cpu_threshold_percent(logical_cpu_count)` 计算，位置在 `core/pressure.py`。
- `foreground_process_cpu_percent`：由 `foreground_process_cpu_threshold_percent(logical_cpu_count)` 计算，位置在 `core/pressure.py`。
- `background_process_ram_gb`：由 `background_process_ram_threshold_gb(total_ram_gb)` 计算，位置在 `core/pressure.py`。
- `foreground_process_ram_gb`：由 `foreground_process_ram_threshold_gb(total_ram_gb)` 计算，位置在 `core/pressure.py`。

### 一个例子

用户问“后台 CPU 警戒线默认值怎么算”，主要看这里：

- `core/pressure.py -> background_process_cpu_threshold_percent(logical_cpu_count)`：单个后台进程的默认 CPU 警戒线。
- `core/pressure.py -> background_total_cpu_threshold_percent(logical_cpu_count)`：后台总 CPU 压力线，也是整机系统 CPU 压力默认线的来源之一。

## 2. 指数退却数组的主要位置和使用接口

### 主要数组位置

位于 `ui/main_window.py` 顶部常量：

- `RISK_ALERT_INTERVALS_S = [15, 30, 60, 120, 240, 480, 960, 1800]`
  作用：给 `系统压力` 和 `轻微干扰` 这一类慢节奏报告使用。
- `STUTTER_ALERT_INTERVALS_S = [2, 4, 8, 16, 32, 64]`
  作用：给普通 `卡顿报告` 使用。
- `DISPLAY_PIPELINE_ALERT_INTERVALS_S = [10, 20, 40, 60, 90]`
  作用：给 `卡顿报告` 里的 `DISPLAY_PIPELINE` 单独使用。

### 退却实现位置

- `core/pressure.py -> class ExponentialBackoffGate`
  作用：离散事件的“每个 key 单独冷却”门控。
- `core/pressure.py -> class PressureAlertScheduler`
  作用：持续压力状态的进入、维持、恢复调度器。

### UI 侧接入点

- `ui/main_window.py -> _report_gate_for_event(event)`
  作用：为不同事件选择该走哪一套退却数组。
- `ui/main_window.py -> _cooldown_key_for_event(event, findings)`
  作用：决定冷却 key 如何分组，避免所有报告互相串门。
- `ui/main_window.py -> _emit_report_event(...)`
  作用：真正发出报告前做退却检查。
- `ui/main_window.py -> _pressure_alert_scheduler`
  作用：系统压力的持续状态调度。
- `ui/main_window.py -> _stutter_pressure_scheduler`
  作用：卡顿期间如果同时叠加了资源压力，用于压力侧节奏控制。

## 3. 各个报告生成算法的位置

### 总体分流位置

- `ui/main_window.py -> _resolve_detection_source(...)`
  作用：把结果分到 `frame / compat / minor / pressure / compat_pressure / system`，也就是最终 UI 中的 `卡顿报告 / 轻微干扰 / 系统压力`。
- `ui/main_window.py -> _bucket_label_for_source(...)`
  作用：把内部 source 映射到中文栏位名。

### 卡顿报告

- 入口：`ui/main_window.py -> _on_frame_stutter_ended(episode)`。
- 帧检测：`core/frame_detector.py`。
- 帧归因：`core/frame_attribution.py`。
- 上下文精炼和系统规则覆盖：`core/analyzer.py -> analyze_frame_episode(...)`。
- 展示文案：`ui/detail_panel.py -> _cause_text_zh(...)`。

简短思路：

- 先由 `frame_detector.py` 或 `compat_detector.py` 认定“确实发生了玩家可感知的异常”。
- 再由 `frame_attribution.py` 看更像 CPU、GPU、驱动路径还是上屏链路。
- 然后由 `analyzer.py` 结合系统快照做二次精炼，例如把 `CPU_BOUND` 精炼成更像 `CPU_STAGE_STALL` 或 `TRANSIENT_DISTURBANCE`。
- 若最终属于重卡顿路径，就进 `卡顿报告`。

### 轻微干扰

- 分流判断：`ui/main_window.py -> _is_minor_frame_episode(...)`。
- `DISPLAY_PIPELINE` 轻重分流：`ui/main_window.py -> _is_minor_display_pipeline(...)`。
- 实际事件入口仍然是 `ui/main_window.py -> _on_frame_stutter_ended(episode)`。
- 轻微事件来源主要仍由 `core/frame_detector.py` 产生，再被 UI 层重分桶。

简短思路：

- 如果异常是真实存在的，但更像轻度尖峰、短时连续抖动、轻微上屏异常或弱证据事件，就不放进重卡顿，而是落到 `轻微干扰`。
- 这样可以减少“明明游戏总体流畅，但重报告一直弹”的问题。

### 系统压力

- 压力评估：`core/pressure.py -> evaluate_pressure(...)`。
- 压力事件入口：`ui/main_window.py -> _record_pressure_risk(sample, findings)`。
- 持续压力节奏：`ui/main_window.py` 中的 `PressureAlertScheduler`。
- 压力总结文案：`core/pressure.py -> summarize_pressure_findings(...)`。
- 兼容模式压力事件：`core/compat_detector.py`。

简短思路：

- 系统压力不要求先出现明显帧卡顿。
- 只要 CPU、RAM、前后台进程、显存、IO 等压力线被触发，就可以形成风险报告。
- 若兼容模式下只看到压力和中等响应延迟，也会走压力路径，而不是混入卡顿报告。

## 4. 高精度模式和兼容模式获取的参数

### 高精度模式

主要文件：`core/presentmon_bridge.py`、`core/models.py`。

- `frame_time_ms`：一帧总耗时。
- `cpu_busy_ms`：这一帧里 CPU 真正在干活的时间。
- `cpu_wait_ms`：这一帧里 CPU 处于等待或阻塞的时间。
- `gpu_busy_ms`：GPU 真正在忙的时间。
- `gpu_wait_ms`：GPU 空等或未忙的时间。
- `gpu_time_ms`：该帧在 GPU 队列中的总占用时间。
- `gpu_latency_ms`：GPU 相关延迟指标。
- `displayed_time_ms`：该帧实际在屏幕上停留了多久。
- `was_displayed`：该帧是否真的上屏；若没上屏，可用于掉帧判断。
- `animation_error_ms`：该帧与理想平滑节奏的偏差。
- `input_latency_ms`：输入到光子的链路延迟，只有可用时才有意义。
- `click_latency_ms`：点击到上屏的链路延迟。
- `flip_delay_ms`：翻转或显示链路相关延迟。
- `present_mode`：Present 路径模式，比如独占、合成等。
- `sync_interval`：同步间隔信息。
- `allows_tearing`：是否允许 tearing。
- `runtime`：D3D、OpenGL、Vulkan 等运行时上下文。
- `capture_time_s`：PresentMon 提供的采集相对时间。

高精度模式下，程序还会同时使用系统采样：

- `cpu_percent`：整机 CPU。
- `cpu_per_core`：每核心 CPU。
- `ram_percent / ram_used_mb / ram_total_mb / ram_available_mb`：系统内存状态。
- `swap_percent`：交换分区压力。
- `responsiveness_ms`：系统响应性延迟。
- `top_processes`：峰值时刻高占用进程列表。
- `target_process`：游戏进程自己的 CPU、内存、IO、线程数等。
- `gpu_memory`：本地显存和共享显存的预算与占用。

### 兼容模式

主要文件：`core/compat_capture.py`、`core/models.py`。

- `is_hung`：窗口是否处于未响应状态。
- `response_time_ms`：窗口消息响应延迟。
- `visual_hash`：窗口画面的轻量哈希值。
- `visual_change_ratio`：画面变化比例，用来判断是不是长时间不变。
- `visual_frozen_streak`：连续多少次采样几乎没变化，用来判断视觉冻结。
- `process_cpu_percent`：目标进程 CPU 占用。
- `process_memory_mb`：目标进程内存占用。
- `process_read_kb_s`：目标进程读取吞吐。
- `process_write_kb_s`：目标进程写入吞吐。
- `thread_count`：目标进程线程数。
- `hwnd / window_title / is_foreground`：当前窗口句柄、标题、是否前台。

兼容模式没有真实逐帧数据，所以它更适合判断：

- 窗口挂起。
- 画面冻结。
- 响应尖峰。
- 进程 CPU 或 IO 压力。

## 关键文件速查

- `main.py`：线程装配和程序入口。
- `ui/main_window.py`：UI 主逻辑、报告分流、退却接线、设置页。
- `ui/detail_panel.py`：右侧报告中文文案和显示结构。
- `ui/event_log.py`：左侧三类报告列表和筛选按钮。
- `core/frame_detector.py`：帧级卡顿识别。
- `core/frame_attribution.py`：帧级归因。
- `core/analyzer.py`：规则仲裁和解释精炼。
- `core/pressure.py`：默认阈值、压力评估、指数退却基础类。
- `core/compat_capture.py`：兼容模式采集。
- `core/compat_detector.py`：兼容模式检测。
- `core/collectors.py`：系统采样。
- `core/storage.py`：事件和快照持久化。
