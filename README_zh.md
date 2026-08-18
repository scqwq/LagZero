# LagLens v1.0

> **让你清楚地知道你的电脑为什么卡了——在卡顿发生之前就捕获了完整的取证上下文。**

LagLens 是一款 Windows 桌面工具，可实时监控系统并自动诊断电脑卡顿。与任务管理器不同（它只能告诉你**现在**正在发生什么），LagLens 会在卡顿事件触发前捕获 **5 秒钟** 的历史数据，并用通俗易懂的语言告诉你卡顿的原因。

---

## Windows 用户（无需 Python）

**从 [Releases](../../releases) 页面下载 `LagLens.exe` 并运行即可。仅此而已。**

- 无需安装
- 无需 Python 环境
- 双击即开始监控
- 关闭窗口后最小化到系统托盘

---

## Linux / macOS 用户（从源码运行）

```bash
git clone https://github.com/YOUR_USERNAME/laglens.git
cd laglens
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

**环境要求：** Python 3.10+

---

## 它能做什么

### 实时仪表盘

| 指标 | 测量内容 |
|---|---|
| CPU % | 整体处理器使用率 |
| RAM % | 已用内存占总内存的比例、交换空间压力 |
| 响应性 | 一个简单 OS 操作的耗时 (ms) —— 能捕捉到 CPU% 无法反映的磁盘瓶颈导致的卡顿 |
| 卡顿评分 | 综合 0–100% 健康度指示器 |

状态点颜色：🟢 正常 → 🟡 偏高 → 🔴 检测到卡顿

### 智能检测

- 在大约 60 秒内学习**你的机器**的正常行为（游戏本 ≠ 办公笔记本）
- 连续 2 秒异常才会触发事件——单次波动会被忽略
- 保留 5 秒卡顿前的滚动缓冲区，以便在卡顿触发时**回溯**原因

### 原因诊断

每个确认的卡顿事件都会得到通俗易懂的解释：

| 原因 | 示例输出 |
|---|---|
| CPU 飙升 | *"chrome.exe (PID 4821) 正在消耗 78% CPU，导致系统失去响应。"* |
| 内存耗尽 | *"系统内存已严重不足 (14.2 GB / 16 GB)。操作系统正在将内存写入磁盘（分页）。"* |
| 后台进程集群 | *"6 个后台进程各自消耗 CPU，合计约 52%。"* |
| 磁盘 I/O | *"CPU 正常 (18%) 但响应性为 210ms。可能是磁盘瓶颈——杀毒软件、系统更新或备份。"* |
| 调度器争用 | *"系统处于总体压力状态——未找到单一明确原因。"* |

### 事件历史

- 每次卡顿事件都保存到本地 SQLite 数据库——重启后历史记录依然存在
- 点击任意历史事件可查看：原因卡片、峰值指标、卡顿前时间线、进程排行表

---

## 自己编译 .exe（Windows）

如果你想从源码构建 `LagLens.exe`：

```bat
git clone https://github.com/YOUR_USERNAME/laglens.git
cd laglens
build_windows.bat
```

脚本会自动安装所有依赖并运行 PyInstaller。
输出文件：`dist\LagLens.exe` —— 单个可移植文件。

---

## 架构

```
main.py                 入口点 —— 将所有组件串联起来
core/
  models.py              数据结构 (SystemSample, LagEvent, LagSnapshot)
  collectors.py          后台线程：CPU、RAM、进程、响应性探针
  detection.py           Sigmoid 评分、滚动窗口、基线学习、状态机
  analyzer.py            5 条规则的原因引擎 → 通俗语言解释
  recorder.py            5 秒卡顿前滚动缓冲区 + 快照捕获
  storage.py             SQLite 持久化（通过 SQLAlchemy）
ui/
  main_window.py         实时指标栏、状态点、托盘图标、信号连接
  event_log.py           带严重程度颜色和原因标签的可滚动事件列表
  detail_panel.py        原因卡片、峰值指标、时间线、进程表
```

---

## 测试

```bash
python tests/test_detection.py
# 16 个测试用例 —— 检测引擎、原因分析器、Sigmoid 数学、误报预防
```

---

## 路线图

- [ ] 磁盘 I/O 采集器 (`psutil.disk_io_counters`)
- [ ] 网络流量飙升检测
- [ ] 导出历史记录到 CSV
- [ ] 基于 ML 的分类器（用你收集的事件训练的决策树）
- [ ] 设置面板（自定义阈值、采样间隔）

---

## 许可证

MIT —— 详见 [LICENSE](LICENSE)
