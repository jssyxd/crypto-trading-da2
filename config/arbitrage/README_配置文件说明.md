# 套利系统配置文件完全指南

## 📋 配置文件总览

```
config/arbitrage/
├── 🎯 核心配置（必须理解）
│   ├── arbitrage_segmented.yaml          # 主配置：交易参数、风控、数量
│   ├── multi_exchange_arbitrage.yaml     # 1对多/多对多套利规则
│   ├── multi_leg_pairs.yaml              # 多腿套利规则（如 PAXG↔XAU）
│   └── monitor_v2.yaml                   # 监控配置：交易所、行情订阅
│
├── 📝 场景配置（特定场景使用）
│   ├── monitor_paradex_lighter_btc.yaml  # Paradex↔Lighter BTC套利
│   ├── monitor_lighter_gold.yaml         # Lighter黄金套利
│   └── monitor_lighter_multi_btc.yaml    # Lighter多交易所BTC套利
│
├── 🔧 辅助配置（自动读取，一般不改）
│   ├── extra_symbols.yaml                # 额外订阅的交易对
│   ├── segment_symbol_filters.yaml       # 交易对过滤规则
│   └── monitor.yaml                      # 旧版监控配置（已废弃）
│
└── 📦 其他
    └── arbitrage_unified.yaml            # 统一模式配置（备用）
```

---

## 🎯 三种主要套利模式

### 模式 1️⃣：1对多套利（跨交易所）

**场景**：Lighter 同时与 EdgeX、Backpack、Paradex 套利

**需要修改的配置**：

1. **`multi_exchange_arbitrage.yaml`** - 定义套利规则
```yaml
enabled: true                    # ✅ 必须开启
mode: "one_to_many"             # ✅ 1对多模式
center_exchange: "lighter"      # 中心交易所
counter_exchanges:              # 对手交易所列表
  - "edgex"
  - "backpack"
  - "paradex"
symbols:
  - "BTC-USDC-PERP"            # 要套利的币种
```

2. **`arbitrage_segmented.yaml`** - 配置交易参数
```yaml
system_mode:
  monitor_only: false           # false=实盘，true=仅监控

symbol_configs:
  "BTC-USDC-PERP":              # 为BTC配置参数
    grid_config:
      initial_spread_threshold: 0.1   # 触发价差 0.1%
      grid_step: 0.05                 # 网格间隔
      max_segments: 5                 # 最大格子数
    quantity_config:
      base_quantity: 0.001            # 每格数量
```

3. **`monitor_v2.yaml`** - 监控配置（一般不改）
```yaml
exchanges:                      # 确保包含所有交易所
  - edgex
  - backpack
  - lighter
  - paradex
symbols:                        # 确保包含要套利的币种
  - BTC-USDC-PERP
```

**运行命令**：
```bash
python main_unified.py \
  --config config/arbitrage/arbitrage_segmented.yaml \
  --monitor-config config/arbitrage/monitor_v2.yaml
```

---

### 模式 2️⃣：1对1套利（两个交易所）

**场景**：只做 Paradex ↔ Lighter 的 BTC 套利

**需要修改的配置**：

1. **`multi_exchange_arbitrage.yaml`** - 关闭（不需要）
```yaml
enabled: false                  # ❌ 关闭1对多
```

2. **`arbitrage_segmented.yaml`** - 配置交易参数（同上）

3. **`monitor_paradex_lighter_btc.yaml`** - 使用专用监控配置
```yaml
exchanges:
  - lighter
  - paradex
symbols:
  - BTC-USDC-PERP
```

**运行命令**：
```bash
python main_unified.py \
  --config config/arbitrage/arbitrage_segmented.yaml \
  --monitor-config config/arbitrage/monitor_paradex_lighter_btc.yaml
```

---

### 模式 3️⃣：多腿套利（同交易所内）

**场景**：Lighter 交易所内 PAXG ↔ XAU 套利

**需要修改的配置**：

1. **`multi_leg_pairs.yaml`** - 定义多腿规则
```yaml
enabled: true
pairs:
  - pair_id: "LIGHTER_PAXG_XAU"
    description: "Lighter PAXG↔XAU 同所套利"
    exchange: "lighter"
    legs:
      - symbol: "PAXG-USD-PERP"
        direction: "buy"
      - symbol: "XAU-USD-PERP"
        direction: "sell"
```

2. **`arbitrage_segmented.yaml`** - 配置交易参数
```yaml
symbol_configs:
  "LIGHTER_PAXG_XAU":           # 使用 pair_id
    grid_config:
      initial_spread_threshold: 0.05
      grid_step: 0.03
    quantity_config:
      base_quantity: 0.006
```

3. **`monitor_lighter_gold.yaml`** - 使用专用监控配置

**运行命令**：
```bash
python main_unified.py \
  --config config/arbitrage/arbitrage_segmented.yaml \
  --monitor-config config/arbitrage/monitor_lighter_gold.yaml
```

---

## 🔧 配置文件详细说明

### 1. `arbitrage_segmented.yaml` - 主配置文件

**作用**：定义所有交易参数、风控规则

**关键配置项**：

```yaml
# 系统模式
system_mode:
  monitor_only: true              # true=仅监控，false=实盘交易
  data_freshness_seconds: 3.0     # 数据新鲜度（秒）

# 全局默认配置
default_config:
  grid_config:
    initial_spread_threshold: 0.05    # 第1格触发点（%）
    grid_step: 0.03                   # 网格间隔（%）
    max_segments: 5                   # 最大格子数
    spread_persistence_seconds: 5     # 价差持续N秒才触发
    strict_persistence_check: true    # 严格持续性检查
    
  quantity_config:
    base_quantity: 1.0                # 每格基础数量
    quantity_mode: "fixed"            # fixed=固定数量
    quantity_precision: 2             # 数量精度
    
  risk_config:
    max_position_value: 10000.0       # 最大持仓价值（USDC）
    max_loss_percent: 5.0             # 最大亏损百分比

# 针对特定交易对的配置（覆盖默认值）
symbol_configs:
  "BTC-USDC-PERP":
    grid_config:
      initial_spread_threshold: 0.1
    quantity_config:
      base_quantity: 0.001
```

**何时修改**：
- ✅ 切换监控/实盘模式
- ✅ 调整触发价差
- ✅ 调整下单数量
- ✅ 调整风控参数

---

### 2. `multi_exchange_arbitrage.yaml` - 1对多套利规则

**作用**：自动生成跨交易所套利对

**配置说明**：

```yaml
enabled: true                    # 是否启用
mode: "one_to_many"             # one_to_many 或 many_to_many
allow_reverse: true             # 是否自动选择买卖方向
min_spread_pct: 0.1             # 最小价差要求（可选）

center_exchange: "lighter"      # 中心交易所
counter_exchanges:              # 对手交易所列表
  - "edgex"
  - "backpack"
  - "paradex"
  
symbols:                        # 要套利的币种
  - "BTC-USDC-PERP"
  - "ETH-USDC-PERP"
```

**生成结果**：
- `EDGEX_LIGHTER_BTC`
- `BACKPACK_LIGHTER_BTC`
- `LIGHTER_PARADEX_BTC`
- `EDGEX_LIGHTER_ETH`
- `BACKPACK_LIGHTER_ETH`
- `LIGHTER_PARADEX_ETH`

**何时修改**：
- ✅ 启用/禁用1对多套利
- ✅ 更换中心交易所
- ✅ 增减对手交易所
- ✅ 增减套利币种

---

### 3. `multi_leg_pairs.yaml` - 多腿套利规则

**作用**：定义同交易所内的多腿套利

**配置说明**：

```yaml
enabled: true

pairs:
  - pair_id: "LIGHTER_PAXG_XAU"           # 套利对ID
    description: "Lighter PAXG↔XAU 同所套利"
    exchange: "lighter"                    # 交易所
    allow_reverse: true                    # 是否自动选择方向
    
    legs:                                  # 腿配置
      - symbol: "PAXG-USD-PERP"
        direction: "buy"                   # 买入腿
        
      - symbol: "XAU-USD-PERP"
        direction: "sell"                  # 卖出腿
```

**何时修改**：
- ✅ 添加新的多腿套利对
- ✅ 修改腿的方向
- ✅ 启用/禁用多腿套利

---

### 4. `monitor_v2.yaml` - 通用监控配置

**作用**：定义监控行为（交易所、行情订阅、性能参数）

**配置说明**：

```yaml
# 交易所列表
exchanges:
  - edgex
  - backpack
  - lighter
  - paradex

# 监控的交易对
symbols:
  - BTC-USDC-PERP
  - ETH-USDC-PERP

# WebSocket配置
websocket:
  ping_interval: 30
  reconnect_delay: 5
  max_reconnect_attempts: 5

# 性能配置
performance:
  analysis_interval_ms: 10
  ui_refresh_interval_ms: 1000
```

**何时修改**：
- ✅ 增减监控的交易所
- ✅ 增减监控的币种
- ⚠️ 调整性能参数（谨慎）

---

### 5. 场景专用配置文件

| 文件名 | 用途 | 交易所 | 币种 |
|--------|------|--------|------|
| `monitor_paradex_lighter_btc.yaml` | Paradex↔Lighter BTC套利 | lighter, paradex | BTC |
| `monitor_lighter_gold.yaml` | Lighter黄金套利 | lighter | PAXG, XAU |
| `monitor_lighter_multi_btc.yaml` | Lighter多交易所BTC套利 | lighter, edgex, backpack | BTC |

**何时使用**：
- 只想运行特定场景的套利
- 不想监控所有交易所/币种
- 需要专门的配置参数

---

## 🚀 快速启动指南

### 场景 A：我想做 Lighter 对多个交易所的 BTC 套利

1. **修改 `multi_exchange_arbitrage.yaml`**：
```yaml
enabled: true
mode: "one_to_many"
center_exchange: "lighter"
counter_exchanges: ["edgex", "backpack", "paradex"]
symbols: ["BTC-USDC-PERP"]
```

2. **修改 `arbitrage_segmented.yaml`**：
```yaml
system_mode:
  monitor_only: false  # 实盘模式

symbol_configs:
  "BTC-USDC-PERP":
    quantity_config:
      base_quantity: 0.001  # 设置你的数量
```

3. **运行**：
```bash
python main_unified.py \
  --config config/arbitrage/arbitrage_segmented.yaml \
  --monitor-config config/arbitrage/monitor_v2.yaml
```

---

### 场景 B：我只想监控价差，不下单

1. **修改 `arbitrage_segmented.yaml`**：
```yaml
system_mode:
  monitor_only: true  # 仅监控
```

2. **运行**（任选其一）：
```bash
# 方式1：使用执行脚本（但不会下单）
python main_unified.py \
  --config config/arbitrage/arbitrage_segmented.yaml \
  --monitor-config config/arbitrage/monitor_v2.yaml

# 方式2：使用纯监控脚本
python run_arbitrage_monitor_v2.py \
  --config config/arbitrage/monitor_v2.yaml
```

---

### 场景 C：我想做 Lighter 黄金套利（PAXG↔XAU）

1. **修改 `multi_leg_pairs.yaml`**：
```yaml
enabled: true
pairs:
  - pair_id: "LIGHTER_PAXG_XAU"
    exchange: "lighter"
    legs:
      - symbol: "PAXG-USD-PERP"
        direction: "buy"
      - symbol: "XAU-USD-PERP"
        direction: "sell"
```

2. **修改 `arbitrage_segmented.yaml`**：
```yaml
symbol_configs:
  "LIGHTER_PAXG_XAU":
    quantity_config:
      base_quantity: 0.006
```

3. **运行**：
```bash
python main_unified.py \
  --config config/arbitrage/arbitrage_segmented.yaml \
  --monitor-config config/arbitrage/monitor_lighter_gold.yaml
```

---

## ⚠️ 常见问题

### Q1: 我修改了配置，但没生效？
**A**: 确保：
- ✅ 配置文件格式正确（YAML语法）
- ✅ `enabled: true`（如果有这个字段）
- ✅ 重启了脚本
- ✅ 检查日志中是否有加载成功的提示

### Q2: 我应该修改哪个配置文件？
**A**: 
- **交易参数/数量/风控** → `arbitrage_segmented.yaml`
- **1对多套利规则** → `multi_exchange_arbitrage.yaml`
- **多腿套利规则** → `multi_leg_pairs.yaml`
- **监控的交易所/币种** → `monitor_v2.yaml` 或场景专用配置

### Q3: 多个配置文件冲突怎么办？
**A**: 优先级：
1. `symbol_configs` 中的特定配置
2. `default_config` 中的默认配置
3. 系统内置默认值

### Q4: 我可以同时运行多个套利模式吗？
**A**: 可以！
- ✅ 1对多 + 多腿套利（同时启用）
- ✅ 多个币种的套利（在 symbols 中添加）
- ⚠️ 注意风控和资金分配

---

## 📝 配置检查清单

运行前检查：

- [ ] `.env` 文件中已配置所有交易所的 API 密钥
- [ ] `arbitrage_segmented.yaml` 中 `monitor_only` 设置正确
- [ ] 下单数量符合交易所最小精度要求
- [ ] 风控参数设置合理（最大持仓、最大亏损）
- [ ] 监控配置中包含所需的交易所和币种
- [ ] 套利规则配置已启用（`enabled: true`）

---

## 🆘 需要帮助？

如果还有疑问，请提供：
1. 你想运行的套利场景
2. 涉及的交易所和币种
3. 是监控还是实盘

我会为你准备具体的配置方案！

