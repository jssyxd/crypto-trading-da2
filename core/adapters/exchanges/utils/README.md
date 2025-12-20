# 交易所适配器工具模块说明

本目录包含交易所适配器使用的各种工具类和辅助函数。

## 📋 工具列表

### ✅ 已使用

#### 1. `cache_config.py` - 统一缓存配置
**状态**: ✅ **已使用**  
**用途**: 统一管理所有交易所适配器的缓存TTL配置

**使用位置**:
- `lighter.py`: 余额和持仓缓存TTL
- `backpack.py`: 余额刷新间隔
- `edgex.py`: 余额刷新间隔

**使用示例**:
```python
from ..utils.cache_config import get_cache_ttl, get_balance_refresh_interval

balance_ttl = get_cache_ttl('balance')  # 获取余额缓存TTL（60秒）
refresh_interval = get_balance_refresh_interval()  # 获取余额刷新间隔（15秒）
```

**配置项**:
- `balance`: 余额缓存TTL（60秒）
- `position`: 持仓缓存TTL（30秒）
- `orderbook`: 订单簿缓存TTL（5秒）
- `ticker`: Ticker缓存TTL（10秒）
- `market_info`: 市场信息缓存TTL（300秒）
- `user_stats`: 用户统计缓存TTL（180秒）
- `BALANCE_REFRESH_INTERVAL`: 余额自动刷新间隔（15秒）

---

### ⚠️ 未使用（可选工具）

#### 2. `adapter_logger.py` - 统一日志工具
**状态**: ⚠️ **未使用**（可选）  
**用途**: 提供统一的日志格式化接口，确保日志清晰、可读性强

**功能**:
- 统一的日志消息模板
- 日志聚合（减少重复日志）
- 自动日志级别管理
- 预定义的日志格式

**使用示例**:
```python
from ..utils.adapter_logger import AdapterLogger

# 初始化
adapter_logger = AdapterLogger(self.logger, "Lighter")

# 使用
adapter_logger.balance_success(count=1)
adapter_logger.error_rate_limit(retry_after=60)
adapter_logger.heartbeat_ping()
```

**优势**:
- ✅ 统一日志格式
- ✅ 自动日志聚合
- ✅ 减少重复代码

**何时使用**:
- 新开发的适配器可以直接使用
- 现有适配器可以逐步迁移（可选）

---

#### 3. `cache_manager.py` - 统一缓存管理器
**状态**: ⚠️ **未使用**（可选）  
**用途**: 提供统一的缓存管理功能，包括缓存存储、检索、自动过期检查

**功能**:
- 统一的缓存存储和检索接口
- 自动过期检查
- 缓存统计和清理
- 支持多种缓存类型

**使用示例**:
```python
from ..utils.cache_manager import ExchangeCacheManager

# 初始化
cache_manager = ExchangeCacheManager(exchange_id="lighter")

# 设置缓存
cache_manager.set('balance', 'USDC', balance_data, ttl=60)

# 获取缓存
balance = cache_manager.get('balance', 'USDC')

# 清理过期缓存
cleaned = cache_manager.cleanup_expired('balance')

# 获取统计信息
stats = cache_manager.get_stats()
```

**优势**:
- ✅ 统一管理所有缓存类型
- ✅ 自动过期检查
- ✅ 缓存统计和监控

**何时使用**:
- 新开发的适配器可以直接使用
- 现有适配器可以逐步迁移（可选）

---

#### 4. `reconnect_manager.py` - 统一重连管理器
**状态**: ⚠️ **未使用**（可选）  
**用途**: 提供统一的WebSocket重连逻辑，包括指数退避、重连次数限制、网络连通性检查

**功能**:
- 统一的重连逻辑
- 支持多种退避策略（指数、线性、固定）
- 网络连通性检查（可选）
- 重连状态管理和统计

**使用示例**:
```python
from ..utils.reconnect_manager import (
    WebSocketReconnectManager,
    ReconnectConfig,
    ReconnectStrategy
)

# 创建配置
config = ReconnectConfig(
    max_retries=10,
    base_delay=2.0,
    max_delay=300.0,
    strategy=ReconnectStrategy.EXPONENTIAL,
    enable_network_check=True
)

# 初始化管理器
reconnect_manager = WebSocketReconnectManager(
    exchange_id="edgex",
    config=config,
    network_check_func=lambda: check_network()
)

# 执行重连
success = await reconnect_manager.reconnect(
    connect_func=self._connect_websocket,
    cleanup_func=self._cleanup_old_connection
)
```

**优势**:
- ✅ 统一的重连策略
- ✅ 可配置的重连参数
- ✅ 网络连通性检查
- ✅ 重连统计和监控

**何时使用**:
- 新开发的适配器可以直接使用
- 现有适配器可以逐步迁移（可选）

---

#### 5. `error_handler.py` - 统一错误处理
**状态**: ⚠️ **未使用**（可选）  
**用途**: 提供统一的API错误处理和重试机制

**功能**:
- 统一的API错误分类
- 自动重试机制
- 错误分类和日志记录
- 可配置的重试策略

**使用示例**:
```python
from ..utils.error_handler import (
    exchange_api_retry,
    ErrorCategory,
    handle_exchange_error
)

class BackpackRest:
    @exchange_api_retry(
        max_retries=3,
        backoff_base=1.0,
        retry_on=(ErrorCategory.NETWORK, ErrorCategory.SERVER_ERROR)
    )
    async def fetch_balances(self):
        try:
            response = await self._make_request(...)
            return response
        except Exception as e:
            handle_exchange_error(e, "fetch_balances", "backpack", self.logger)
            raise
```

**错误分类**:
- `NETWORK`: 网络错误
- `AUTHENTICATION`: 认证错误
- `RATE_LIMIT`: 限流错误
- `SERVER_ERROR`: 服务器错误
- `CLIENT_ERROR`: 客户端错误
- `UNKNOWN`: 未知错误

**优势**:
- ✅ 统一的错误处理逻辑
- ✅ 自动重试机制
- ✅ 错误分类和日志记录

**何时使用**:
- 新开发的适配器可以直接使用
- 现有适配器可以逐步迁移（可选）

---

### 📝 其他工具

#### 6. `log_formatter.py` - 日志格式化器
**状态**: ✅ **已使用**  
**用途**: 提供多种日志格式化器（简洁、详细、彩色）

**使用位置**:
- `setup_logging.py`: 使用这些格式化器

**格式化器类型**:
- `CompactFormatter`: 简洁格式（终端）
- `DetailedFormatter`: 详细格式（文件）
- `ColoredFormatter`: 彩色格式（终端）

---

#### 7. `setup_logging.py` - 日志配置工具
**状态**: ✅ **已使用**  
**用途**: 提供统一的日志配置接口，支持多种格式化器

**使用位置**:
- 适配器初始化时调用

**功能**:
- 统一的日志配置接口
- 支持多种格式化器
- 文件和控制台日志分离

---

## 🎯 使用建议

### 当前策略
1. **已使用的工具**: 继续使用，保持现状
2. **未使用的工具**: 作为可选工具，新适配器可以直接使用

### 迁移建议
如果需要统一管理，可以逐步迁移现有适配器使用这些工具：

1. **优先迁移**: `adapter_logger.py`（统一日志格式）
2. **可选迁移**: `cache_manager.py`（统一缓存管理）
3. **可选迁移**: `reconnect_manager.py`（统一重连逻辑）
4. **可选迁移**: `error_handler.py`（统一错误处理）

### 新适配器开发
新开发的适配器建议直接使用这些工具：
- ✅ 使用 `cache_config.py` 获取缓存配置
- ✅ 使用 `adapter_logger.py` 统一日志格式
- ✅ 使用 `cache_manager.py` 管理缓存
- ✅ 使用 `reconnect_manager.py` 处理重连
- ✅ 使用 `error_handler.py` 处理错误

---

## 📊 工具使用状态总结

| 工具 | 状态 | 使用位置 | 优先级 |
|------|------|----------|--------|
| `cache_config.py` | ✅ 已使用 | lighter.py, backpack.py, edgex.py | 高 |
| `log_formatter.py` | ✅ 已使用 | setup_logging.py | 高 |
| `setup_logging.py` | ✅ 已使用 | 适配器初始化 | 高 |
| `adapter_logger.py` | ⚠️ 未使用 | 无 | 中 |
| `cache_manager.py` | ⚠️ 未使用 | 无 | 中 |
| `reconnect_manager.py` | ⚠️ 未使用 | 无 | 低 |
| `error_handler.py` | ⚠️ 未使用 | 无 | 低 |

---

**最后更新**: 2025-11-17  
**文档版本**: v1.0

