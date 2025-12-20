# monitor_lighter_eth_spot.yaml 参数完整分析报告

## 📋 分析方法

通过代码追踪 `unified_orchestrator.py` 中对 `self.monitor_config.*` 的所有引用，确定实际使用的参数。

## ✅ 有效参数（已保留）

| 参数 | 代码位置 | 用途 |
|------|---------|------|
| `exchanges` | unified_orchestrator.py:631 | 传递给UI管理器，显示交易所列表 |
| `symbols` | unified_orchestrator.py:632,831,2699 | 传递给UI管理器，主循环交易对列表 |
| `performance.ui_refresh_interval_ms` | unified_orchestrator.py:640 | UI刷新循环间隔时间 |
| `debug_cli.enabled` | unified_orchestrator.py:619 | 是否启用CLI调试模式（关闭富UI） |
| `debug_cli.interval_seconds` | unified_orchestrator.py:650 | CLI打印间隔时间 |

### 补充说明

**队列大小参数**（虽然未在配置文件定义，但确实被使用）：
- `orderbook_queue_size`: 默认1000（unified_orchestrator.py:137）
- `ticker_queue_size`: 默认500（unified_orchestrator.py:138）

如需修改队列大小，可在配置文件添加：
```yaml
queues:
  orderbook_queue_size: 1000
  ticker_queue_size: 500
```

但由于 `monitor_config.py` 的 `_load_from_file` 方法没有读取 `queues` section，需要修改加载逻辑才能生效。

---

## ❌ 无效参数（已删除）

### 1. 套利阈值配置（2个参数）
```yaml
thresholds:
  min_spread_pct: 0.02          # ❌ 未使用
  min_funding_rate_diff: 0.01   # ❌ 未使用
```

**删除原因**：
- `unified_orchestrator.py` 完全不使用这两个参数
- 多腿套利使用 `arbitrage_segmented.yaml` 中的阈值配置
- 跨所套利使用 `multi_exchange_arbitrage.yaml` 中的阈值配置

**验证命令**：
```bash
grep -r "min_spread_pct\|min_funding_rate_diff" core/services/arbitrage_monitor_v2/core/unified_orchestrator.py
# 结果：No matches found
```

---

### 2. WebSocket配置（3个参数）
```yaml
websocket:
  ping_interval: 30             # ❌ 未使用
  reconnect_delay: 5            # ❌ 未使用
  max_reconnect_attempts: 50000 # ❌ 未使用
```

**删除原因**：
- 这些参数被 `monitor_config.py` 读取到 `self.config.ws_*` 中
- 但 `unified_orchestrator.py` 从未访问这些属性
- WebSocket 连接由交易所适配器（`ExchangeAdapter`）直接管理
- 适配器使用自己的配置，不读取 `monitor_config`

**验证命令**：
```bash
grep -r "ws_ping_interval\|ws_reconnect_delay\|ws_max_reconnect_attempts" \
  core/services/arbitrage_monitor_v2/core/unified_orchestrator.py
# 结果：No matches found
```

---

### 3. 性能配置（1个参数）
```yaml
performance:
  analysis_interval_ms: 10      # ❌ 未使用
```

**删除原因**：
- `unified_orchestrator.py` 使用**事件驱动模式**
- 不使用固定间隔轮询（`asyncio.sleep`）
- 此参数仅在旧版 `orchestrator.py` 中使用

**验证命令**：
```bash
grep -r "analysis_interval_ms" core/services/arbitrage_monitor_v2/core/unified_orchestrator.py
# 结果：No matches found
```

**对比**：
- `orchestrator.py:442`：`await asyncio.sleep(self.config.analysis_interval_ms / 1000)` ✅ 使用
- `unified_orchestrator.py`：无此代码 ❌ 不使用

---

### 4. 健康检查配置（2个参数）
```yaml
health_check:
  interval: 10                  # ❌ 未使用
  data_timeout: 30              # ❌ 未使用
```

**删除原因**：
- `unified_orchestrator.py` 没有初始化 `HealthMonitor` 模块
- 没有调用 `await self.health_monitor.start()`
- 这些参数仅在旧版 `orchestrator.py` 中使用

**验证命令**：
```bash
grep -r "health_check_interval\|data_timeout_seconds" \
  core/services/arbitrage_monitor_v2/core/unified_orchestrator.py
# 结果：No matches found

grep -r "HealthMonitor" core/services/arbitrage_monitor_v2/core/unified_orchestrator.py
# 结果：No matches found
```

**对比**：
- `orchestrator.py:98,167`：使用 `HealthMonitor` ✅ 使用
- `unified_orchestrator.py`：无 `HealthMonitor` 导入或初始化 ❌ 不使用

---

### 5. 历史记录配置（10个参数）
```yaml
spread_history:
  enabled: false                # ❌ 未使用
  data_dir: "data/spread_history"
  sampling:
    interval_seconds: 60        # ❌ 未使用
    strategy: "max"             # ❌ 未使用
  write:
    batch_size: 10              # ❌ 未使用
    batch_timeout: 60.0         # ❌ 未使用
    queue_maxsize: 500          # ❌ 未使用
  storage:
    compress_after_days: 10     # ❌ 未使用
    archive_after_days: 30      # ❌ 未使用
    cleanup_interval_hours: 24  # ❌ 未使用
```

**删除原因**：
- `unified_orchestrator.py` 没有导入 `SpreadHistoryRecorder`
- 没有初始化历史记录模块
- 即使设置 `enabled: true` 也不会记录任何数据
- 历史记录功能仅在 `arbitrage_orchestrator_v3.py` 中实现

**验证命令**：
```bash
grep -r "spread_history\|SpreadHistoryRecorder" \
  core/services/arbitrage_monitor_v2/core/unified_orchestrator.py
# 结果：No matches found
```

**对比**：
- `arbitrage_orchestrator_v3.py:48`：`from ..history.spread_history_recorder import SpreadHistoryRecorder` ✅ 使用
- `unified_orchestrator.py`：无此导入 ❌ 不使用

---

## 📊 统计结果

| 状态 | 参数数量 | 百分比 |
|------|---------|--------|
| ✅ 有效参数 | 5 | 21.7% |
| ❌ 无效参数 | 18 | 78.3% |
| **总计** | **23** | **100%** |

---

## 🎯 最终配置文件

精简后的配置文件仅保留5个有效参数：

```yaml
# 交易所配置
exchanges:
  - lighter

# 监控的交易对
symbols: []

# UI刷新配置
performance:
  ui_refresh_interval_ms: 1000

# Debug CLI 模式配置
debug_cli:
  enabled: false
  interval_seconds: 1.0
```

---

## 📝 其他配置文件说明

如需修改套利系统的其他参数，请查看：

| 配置文件 | 用途 |
|---------|------|
| `arbitrage_segmented.yaml` | 网格配置、开平仓阈值、数量配置、风险控制 |
| `arbitrage_unified.yaml` | 全局风险控制、执行模式、交易所手续费 |
| `multi_leg_pairs.yaml` | 多腿套利交易对配置 |
| `extra_symbols.yaml` | 额外订阅的交易对（支持多腿套利） |

---

## ✅ 验证结果

所有分析基于以下代码追踪：

```bash
# 查找 unified_orchestrator.py 中所有使用 monitor_config 的位置
grep -n "self.monitor_config\." \
  core/services/arbitrage_monitor_v2/core/unified_orchestrator.py

# 结果（6处引用）：
# 137: self.orderbook_queue = asyncio.Queue(maxsize=self.monitor_config.orderbook_queue_size)
# 138: self.ticker_queue = asyncio.Queue(maxsize=self.monitor_config.ticker_queue_size)
# 619: debug_cli_enabled = getattr(self.monitor_config, "debug_cli_mode", False)
# 631: 'exchanges': self.monitor_config.exchanges,
# 632: 'symbols': self.monitor_config.symbols,
# 640: self.ui_manager.update_loop(self.monitor_config.ui_refresh_interval_ms)
# 650: self.monitor_config, "debug_cli_interval_seconds", 1.0
# 831: for symbol in self.monitor_config.symbols:
# 2699: symbol_pool: Set[str] = set(self.monitor_config.symbols)
```

---

## 📅 分析日期

2025-12-09

## 🔍 分析方法

1. 读取 `unified_orchestrator.py` 完整代码
2. 搜索所有 `self.monitor_config.*` 引用
3. 对比 `monitor_config.py` 中定义的所有参数
4. 确认哪些参数被实际使用
5. 删除所有未使用的参数
6. 添加注释说明参数用途和代码位置

---

## ⚠️ 重要提醒

修改配置文件后，需要**重启进程**才能生效：

```bash
# 停止当前进程
Ctrl + C

# 重新启动
bash run_eth_spot_arbitrage.sh
```
