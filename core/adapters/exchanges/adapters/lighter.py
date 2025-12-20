"""
Lighter交易所适配器

基于MESA架构的Lighter适配器，提供统一的交易接口。
使用Lighter SDK进行API交互和WebSocket连接。
整合了分离的模块：lighter_base.py、lighter_rest.py、lighter_websocket.py 
""" 

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from decimal import Decimal, InvalidOperation
import yaml
import os

from ....logging import get_logger

from ..adapter import ExchangeAdapter
from ..interface import ExchangeConfig
from ..models import *
from ..subscription_manager import create_subscription_manager, DataType
from .lighter_base import LighterBase
from .lighter_rest import LighterRest
from .lighter_websocket import LighterWebSocket


class LighterAdapter(ExchangeAdapter):
    """Lighter交易所适配器 - 统一接口"""

    def __init__(self, config: ExchangeConfig, event_bus=None):
        super().__init__(config, event_bus)

        # 初始化各个模块
        config_dict = self._convert_config_to_dict(config)
        # 保存原始配置，便于局部重建
        self._config_dict = config_dict
        self._base = LighterBase(config_dict)
        self._rest = LighterRest(config_dict)
        self._websocket = LighterWebSocket(config_dict)
        # 由 orchestrator 注入
        self._backoff_controller = None
        
        # 🔥 优化：将WebSocket引用传递给REST（用于缓存订单簿）
        self._rest.ws = self._websocket

        # 共享数据缓存
        shared_position_cache: Dict[str, Dict[str, Any]] = {}
        shared_order_cache: Dict[str, OrderData] = {}
        shared_order_cache_by_symbol: Dict[str, Dict[str, OrderData]] = {}
        shared_balance_cache: Dict[str, Dict[str, Any]] = {}

        self._position_cache = shared_position_cache
        self._order_cache = shared_order_cache
        self._order_cache_by_symbol = shared_order_cache_by_symbol
        self._balance_cache = shared_balance_cache
        
        # 🔥 余额缓存过期日志频率控制（避免UI抖动）
        self._last_balance_expired_log_time = 0
        self._balance_expired_log_interval = 60  # 每60秒最多打印一次

        # 🔥 将共享缓存传递给 WebSocket 模块（确保缓存一致性）
        self._websocket._position_cache = shared_position_cache
        self._websocket._order_cache = shared_order_cache
        self._websocket._order_cache_by_symbol = shared_order_cache_by_symbol
        self._websocket._balance_cache = shared_balance_cache

        # 设置回调列表
        shared_position_callbacks = []
        shared_order_callbacks = []

        self._position_callbacks = shared_position_callbacks
        self._order_callbacks = shared_order_callbacks

        # 🔥 将共享回调列表传递给 WebSocket 模块
        self._websocket._position_callbacks = shared_position_callbacks
        self._websocket._order_callbacks = shared_order_callbacks

        # 设置基础URL
        self.base_url = getattr(
            config, 'base_url', None) or self._base.base_url
        self.ws_url = getattr(config, 'ws_url', None) or self._base.ws_url
        # 🔒 lighter REST 串行锁（复用执行器同策略，避免nonce冲突）
        self._rest_lock: asyncio.Lock = asyncio.Lock()

        # 符号映射
        self._symbol_mapping = getattr(config, 'symbol_mapping', {})

        # 连接状态
        self._connected = False
        self._authenticated = False

        # 缓存支持的交易对
        self._supported_symbols = []
        self._market_info = {}

        # 初始化订阅管理器
        try:
            config_dict = self._load_lighter_config()

            symbol_cache_service = self._get_symbol_cache_service()

            self._subscription_manager = create_subscription_manager(
                exchange_config=config_dict,
                symbol_cache_service=symbol_cache_service,
                logger=self.logger
            )

            if self.logger:
                mode = config_dict.get('subscription_mode', {}).get('mode', 'unknown')
                self.logger.info(f"[Lighter] 订阅管理器: 初始化成功 (模式: {mode})")

        except Exception as e:
            if self.logger:
                self.logger.warning(f"创建Lighter订阅管理器失败，使用默认配置: {e}")
            # 使用默认配置
            default_config = {
                'exchange_id': 'lighter',
                'subscription_mode': {
                    'mode': 'predefined',
                    'predefined': {
                        'symbols': ['BTC-USD', 'ETH-USD', 'SOL-USD'],
                        'data_types': {'ticker': True, 'orderbook': True, 'trades': False, 'user_data': False}
                    }
                }
            }

            symbol_cache_service = self._get_symbol_cache_service()
            self._subscription_manager = create_subscription_manager(
                exchange_config=default_config,
                symbol_cache_service=symbol_cache_service,
                logger=self.logger
            )

        self.logger.info("[Lighter] 适配器: 初始化完成")

    def _convert_config_to_dict(self, config: ExchangeConfig) -> Dict[str, Any]:
        """
        将ExchangeConfig转换为字典

        如果ExchangeConfig中没有Lighter特有的配置，则从lighter_config.yaml加载

        Args:
            config: ExchangeConfig对象

        Returns:
            配置字典
        """
        # 🔥 优先从配置文件加载完整配置（包括api_config结构）
        try:
            lighter_config = self._load_lighter_config()
            
            # 🔥 检查配置是否加载成功
            if not lighter_config or lighter_config == {'exchange_id': 'lighter'}:
                raise ValueError("配置文件加载失败或为空")
            
            api_config = lighter_config.get('api_config', {})
            
            # 🔥 检查api_config是否存在
            if not api_config:
                if self.logger:
                    self.logger.warning("⚠️ 配置文件中未找到api_config，使用默认配置")
                raise ValueError("api_config不存在")
            
            # 构建配置字典，保持api_config结构
            config_dict = {
                "testnet": api_config.get('testnet', False),
                "api_config": api_config,  # 🔥 保持完整的api_config结构
            }
            
            # 向后兼容：也提供顶层配置项
            auth_config = api_config.get('auth', {})
            if not isinstance(auth_config, dict):
                auth_config = {}
                api_config['auth'] = auth_config
            
            # 🔥 优先使用环境变量（从 ExchangeConfig 传入的值）
            # ExchangeConfigLoader 已经处理了环境变量优先级
            env_private_key = getattr(config, 'api_key_private_key', '') or auth_config.get('api_key_private_key', '')
            env_account_index = getattr(config, 'account_index', 0) or auth_config.get('account_index', 0)
            env_api_key_index = getattr(config, 'api_key_index', 0) or auth_config.get('api_key_index', 0)
            
            # 更新顶层配置（供旧逻辑使用）
            config_dict['api_key_private_key'] = env_private_key
            config_dict['account_index'] = env_account_index
            config_dict['api_key_index'] = env_api_key_index
            
            # ⚙️ 同步更新嵌套的 auth 配置，确保 REST/WebSocket 都能读取到
            if env_private_key:
                auth_config['api_key_private_key'] = env_private_key
            if env_account_index:
                auth_config['account_index'] = env_account_index
            if env_api_key_index:
                auth_config['api_key_index'] = env_api_key_index
            
            # 如果有私钥，自动启用认证
            has_auth = bool(env_private_key)
            auth_enabled = has_auth or auth_config.get('enabled', False)
            auth_config['enabled'] = auth_enabled
            config_dict['auth_enabled'] = auth_enabled
            
            # 🔥 调试：记录配置加载状态（同时输出嵌套auth的值）
            if self.logger:
                api_key_len = len(env_private_key) if env_private_key else 0
                self.logger.info(
                    f"📋 [Lighter配置] auth_enabled={auth_enabled}, account_index={env_account_index}, "
                    f"api_key_index={env_api_key_index}, api_key_len={api_key_len}"
                )
                self.logger.info(
                    "📋 [Lighter配置验证] auth.enabled=%s, account_index=%s, api_key_index=%s",
                    auth_config.get('enabled'),
                    auth_config.get('account_index'),
                    auth_config.get('api_key_index'),
                )
            
            # 🔥 提取WebSocket URL到顶层（供LighterBase使用）
            config_dict['ws_mainnet_url'] = api_config.get('ws_mainnet_url', '')
            config_dict['ws_testnet_url'] = api_config.get('ws_testnet_url', '')
            if 'api_url' in api_config:
                config_dict['api_url'] = api_config['api_url']
            
            if self.logger:
                self.logger.info("✅ 从lighter_config.yaml加载API配置")
        except Exception as e:
            if self.logger:
                self.logger.warning(f"⚠️ 无法从配置文件加载Lighter配置: {e}，使用环境变量配置")
            # 从环境变量构建配置
            api_key_private_key = getattr(config, 'api_key_private_key', '')
            account_index = getattr(config, 'account_index', 0)
            api_key_index = getattr(config, 'api_key_index', 0)
            
            # 🔥 如果环境变量中有私钥，自动启用认证
            auth_enabled = bool(api_key_private_key)
            
            config_dict = {
                "testnet": getattr(config, 'testnet', False),
                "api_key_private_key": api_key_private_key,
                "account_index": account_index,
                "api_key_index": api_key_index,
                "auth_enabled": auth_enabled,
            }
            
            if self.logger:
                self.logger.info(f"📋 [Lighter环境变量配置] auth_enabled={auth_enabled}, account_index={account_index}, api_key_index={api_key_index}")

        # 添加可选配置
        if hasattr(config, 'api_url'):
            config_dict['api_url'] = config.api_url
        if hasattr(config, 'ws_url'):
            config_dict['ws_url'] = config.ws_url

        return config_dict

    def _load_lighter_config(self) -> Dict[str, Any]:
        """加载Lighter配置文件"""
        from pathlib import Path
        config_path = Path("config/exchanges/lighter_config.yaml")

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                if self.logger:
                    self.logger.info(f"✅ [Lighter] 配置文件已加载: {config_path}")
                    # 🔥 调试：验证关键配置项
                    api_config = config.get('api_config', {})
                    auth_config = api_config.get('auth', {})
                    if self.logger:
                        self.logger.info(f"📋 [Lighter配置验证] auth.enabled={auth_config.get('enabled')}, account_index={auth_config.get('account_index')}, api_key_index={auth_config.get('api_key_index')}")
                return config
        except FileNotFoundError:
            if self.logger:
                self.logger.warning(f"⚠️ Lighter配置文件未找到: {config_path}")
            return {'exchange_id': 'lighter'}
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ 加载Lighter配置文件失败: {e}", exc_info=True)
            return {'exchange_id': 'lighter'}

    def _get_symbol_cache_service(self):
        """获取符号缓存服务实例"""
        try:
            # 尝试从依赖注入容器获取符号缓存服务
            from ....di.container import get_container
            from ....services.symbol_manager.interfaces.symbol_cache import ISymbolCacheService

            container = get_container()
            symbol_cache_service = container.get(ISymbolCacheService)

            if self.logger:
                self.logger.debug("[Lighter] 符号缓存服务: 已获取")
            return symbol_cache_service

        except Exception as e:
            if self.logger:
                self.logger.warning(f"⚠️ 获取符号缓存服务失败: {e}，返回None")
            return None

    # ============= 连接管理 =============

    async def connect(self) -> bool:
        """
        建立连接

        Returns:
            是否连接成功
        """
        try:
            if self._connected:
                self.logger.info("已经连接到Lighter")
                return True

            # 初始化REST客户端（即使失败也继续，因为公共数据订阅不需要REST）
            try:
                await self._rest.initialize()
            except Exception as rest_err:
                # 🔥 如果REST初始化失败，记录警告但继续（公共数据模式可能不需要REST）
                self.logger.warning(f"⚠️ Lighter REST客户端初始化失败: {rest_err}，继续尝试WebSocket连接...")
                # 如果是因为账户订阅禁用导致的失败，这是正常的，继续执行
                if "invalid account index" in str(rest_err) or "account index" in str(rest_err).lower():
                    self.logger.info("ℹ️  [Lighter] 这是预期的行为（公共数据模式，不需要REST认证）")

            # 建立WebSocket连接（公共数据订阅只需要WebSocket）
            await self._websocket.connect()

            # 加载市场信息（如果REST可用）
            try:
                await self._load_market_info()
            except Exception as market_err:
                self.logger.warning(f"⚠️ 加载市场信息失败: {market_err}，WebSocket连接已建立，可以继续使用")

            self._connected = True
            self._authenticated = bool(self._rest.signer_client)

            self.logger.info("✅ 成功连接到Lighter交易所")
            return True

        except Exception as e:
            self.logger.error(f"连接Lighter失败: {e}")
            return False

    async def disconnect(self):
        """断开连接"""
        try:
            # 关闭WebSocket
            await self._websocket.disconnect()

            # 关闭REST客户端
            await self._rest.close()

            self._connected = False
            self._authenticated = False

            self.logger.info("已断开与Lighter的连接")

        except Exception as e:
            self.logger.error(f"断开Lighter连接时出错: {e}")

    async def authenticate(self) -> bool:
        """
        进行身份认证（ExchangeInterface标准方法）

        Returns:
            bool: 认证是否成功
        """
        # Lighter的认证在初始化时完成（通过SignerClient）
        # 这里只需要检查是否已经认证
        if self._rest.signer_client:
            self._authenticated = True
            self.logger.info("✅ Lighter认证已完成")
            return True
        else:
            self.logger.warning("⚠️ Lighter未配置SignerClient")
            return False

    async def health_check(self) -> Dict[str, Any]:
        """
        健康检查（ExchangeInterface标准方法）

        Returns:
            Dict: 健康状态信息
        """
        status = {
            "exchange": "lighter",
            "connected": self._connected,
            "authenticated": self._authenticated,
            "timestamp": datetime.now().isoformat()
        }

        try:
            # 尝试获取交易所信息作为健康检查
            if self._connected:
                info = await self.get_exchange_info()
                status["healthy"] = True
                status["market_count"] = len(
                    info.symbols) if info and info.symbols else 0
            else:
                status["healthy"] = False
                status["error"] = "Not connected"
        except Exception as e:
            status["healthy"] = False
            status["error"] = str(e)

        return status

    async def _load_market_info(self):
        """加载市场信息"""
        try:
            exchange_info = await self._rest.get_exchange_info()

            if exchange_info and exchange_info.markets:
                # 🔥 修复：exchange_info.symbols 返回的是字符串列表，不是字典列表
                # 应该使用 exchange_info.markets.values() 来获取市场字典列表
                markets_list = list(exchange_info.markets.values())
                
                self._supported_symbols = [s['symbol'] for s in markets_list]
                self._market_info = {s['symbol']: s for s in markets_list}

                # 更新base模块的市场缓存
                self._base.update_markets_cache(markets_list)

                # 同步到REST和WebSocket模块
                self._rest._markets_cache = self._base._markets_cache
                self._rest._symbol_to_market_index = self._base._symbol_to_market_index
                self._websocket._markets_cache = self._base._markets_cache
                self._websocket._symbol_to_market_index = self._base._symbol_to_market_index

                self.logger.info(f"加载了 {len(self._supported_symbols)} 个交易对")
        except Exception as e:
            self.logger.error(f"加载市场信息失败: {e}", exc_info=True)

    def is_connected(self) -> bool:
        """
        检查是否已连接

        Returns:
            是否已连接
        """
        return self._connected

    # ============= 市场数据 =============

    async def get_exchange_info(self) -> ExchangeInfo:
        """
        获取交易所信息

        Returns:
            ExchangeInfo对象
        """
        return await self._rest.get_exchange_info()

    async def get_ticker(self, symbol: str) -> Optional[TickerData]:
        """
        获取ticker数据

        Args:
            symbol: 交易对符号

        Returns:
            TickerData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_ticker(normalized_symbol)

    async def get_tickers(self, symbols: Optional[List[str]] = None) -> List[TickerData]:
        """
        获取多个交易对行情（ExchangeInterface标准方法）

        Args:
            symbols: 交易对符号列表，None表示获取所有

        Returns:
            List[TickerData]: 行情数据列表
        """
        if symbols is None:
            # 获取所有支持的交易对
            symbols = self._supported_symbols

        tickers = []
        for symbol in symbols:
            try:
                ticker = await self.get_ticker(symbol)
                if ticker:
                    tickers.append(ticker)
            except Exception as e:
                self.logger.error(f"获取ticker失败 {symbol}: {e}")

        return tickers

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Optional[OrderBookData]:
        """
        获取订单簿

        Args:
            symbol: 交易对符号
            limit: 深度限制

        Returns:
            OrderBookData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_orderbook(normalized_symbol, limit)

    async def get_trades(
        self,
        symbol: str,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[TradeData]:
        """
        获取最近成交记录（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            since: 开始时间（暂不支持）
            limit: 数据条数限制

        Returns:
            List[TradeData]: 成交数据列表
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_recent_trades(normalized_symbol, limit or 100)

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[TradeData]:
        """
        获取最近成交（兼容旧接口）

        Args:
            symbol: 交易对符号
            limit: 数量限制

        Returns:
            TradeData列表
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_recent_trades(normalized_symbol, limit)

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[OHLCVData]:
        """
        获取K线数据（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            timeframe: 时间框架（如'1m', '5m', '1h', '1d'）
            since: 开始时间
            limit: 数据条数限制

        Returns:
            List[OHLCVData]: K线数据列表
        """
        # Lighter SDK目前可能不支持K线数据
        # 返回空列表，但记录警告
        self.logger.warning(f"Lighter适配器暂不支持K线数据查询")
        return []

    # ============= 账户信息 =============

    async def get_balances(self) -> List[BalanceData]:
        """
        获取账户余额（ExchangeInterface标准方法）
        
        🎯 策略：完全使用 WebSocket 订阅和缓存
        - 只从 WebSocket 缓存读取余额数据
        - 不降级到 REST API（避免请求频繁错误）
        - 🔥 缓存永不过期：因为余额没有变化时WebSocket不会推送更新
        - 只有在收到新的WebSocket推送时才更新缓存
        - 如果缓存存在（即使时间很长），也使用它

        Returns:
            List[BalanceData]: 余额数据列表（如果缓存可用）
        """
        from datetime import datetime
        
        # 🔥 检查WebSocket对象和缓存
        if not hasattr(self, '_websocket') or not self._websocket:
            if self.logger:
                self.logger.debug("ℹ️ [Lighter] WebSocket对象不存在，等待连接...")
            return []
        
        if not hasattr(self._websocket, '_balance_cache'):
            if self.logger:
                self.logger.debug("ℹ️ [Lighter] WebSocket余额缓存属性不存在，等待初始化...")
            return []
        
        if not self._websocket._balance_cache:
            if self.logger:
                self.logger.debug("ℹ️ [Lighter] WebSocket余额缓存为空，等待WebSocket推送...")
            return []
        
        balance_cache = self._websocket._balance_cache.get('USDC')
        
        if not balance_cache:
            if self.logger:
                self.logger.debug(f"ℹ️ [Lighter] WebSocket余额缓存中没有USDC数据，缓存键: {list(self._websocket._balance_cache.keys())}")
            return []
        
        # 🔥 Lighter余额缓存永不过期：只要缓存存在就使用
        # 原因：余额没有变化时WebSocket不会推送更新，所以缓存应该一直有效
        cache_time = balance_cache.get('timestamp')
        if cache_time:
            cache_age = (datetime.now() - cache_time).total_seconds()
            if self.logger:
                self.logger.debug(f"✅ [Lighter] 使用WebSocket余额缓存 (缓存年龄: {cache_age:.1f}秒，永不过期)")
        else:
            # 即使没有时间戳，也使用缓存（可能是旧版本的数据）
            if self.logger:
                self.logger.debug("✅ [Lighter] 使用WebSocket余额缓存 (无时间戳，但缓存存在)")
        
        return [BalanceData(
            currency='USDC',
            free=balance_cache.get('free', 0),
            used=balance_cache.get('used', 0),
            total=balance_cache.get('total', 0),
            usd_value=balance_cache.get('total', 0),
            timestamp=cache_time if cache_time else datetime.now(),
            raw_data={'source': 'ws', **balance_cache.get('raw_data', {})}  # 🔥 标记来源
        )]

    async def get_account_balance(self) -> List[BalanceData]:
        """
        获取账户余额（兼容旧接口）
        
        🔥 完全使用 WebSocket 订阅，不调用 REST API

        Returns:
            BalanceData列表（从WebSocket缓存）
        """
        return await self.get_balances()

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        获取活跃订单
        
        Args:
            symbol: 交易对符号（可选）
        
        Returns:
            OrderData列表
        """
        normalized_symbol = self._normalize_symbol(symbol) if symbol else None
        cached_orders = self._collect_cached_orders(normalized_symbol)
        ws_cache_ready = bool(getattr(self._websocket, "_order_cache_ready", False))
        
        if ws_cache_ready:
            return cached_orders
        
        if cached_orders:
            # WebSocket已返回部分订单但尚未完成初始化，仍优先返回缓存
            return cached_orders
        
        self.logger.warning(
            "⚠️ [Lighter] WebSocket订单缓存未就绪，临时使用REST查询活跃订单（仅初始化阶段）"
        )
        async with self._rest_lock:
            return await self._rest.get_open_orders(normalized_symbol)
    
    def _collect_cached_orders(self, normalized_symbol: Optional[str]) -> List[OrderData]:
        """
        基于WebSocket缓存返回当前挂单列表
        """
        order_cache_by_symbol: Dict[str, Dict[str, OrderData]] = getattr(
            self, "_order_cache_by_symbol", {}
        )
        if not order_cache_by_symbol:
            return []
        
        if normalized_symbol:
            symbol_cache = order_cache_by_symbol.get(normalized_symbol, {})
            return list(symbol_cache.values())
        
        orders: List[OrderData] = []
        for symbol_cache in order_cache_by_symbol.values():
            orders.extend(symbol_cache.values())
        return orders

    async def get_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:
        """
        获取持仓信息（ExchangeInterface标准方法）

        🎯 优先级策略：
        1. WebSocket 缓存（实时推送，优先）✅
        2. REST API 查询（降级备用）⚠️

        Args:
            symbols: 交易对符号列表，None表示获取所有

        Returns:
            List[PositionData]: 持仓数据列表
        """
        from datetime import datetime
        from decimal import Decimal
        
        # 🔥 策略1: 优先使用 WebSocket 缓存
        if not hasattr(self._websocket, '_position_cache'):
            if self.logger:
                self.logger.debug("ℹ️ [Lighter] WebSocket持仓缓存未初始化，等待账户推送...")
            return []
        
        if not self._websocket._position_cache:
            if self.logger:
                self.logger.debug("ℹ️ [Lighter] WebSocket持仓缓存为空，等待最新推送...")
            return []
        
        from ..models import PositionSide, MarginMode
        cached_positions = []
        target_symbols = symbols if symbols else self._supported_symbols
        
        for symbol in target_symbols:
            cached = self._websocket._position_cache.get(symbol)
            if not cached:
                continue
            
            side = PositionSide.LONG if str(cached.get('side', '')).lower() == 'long' else PositionSide.SHORT
            cached_positions.append(PositionData(
                symbol=symbol,
                side=side,
                size=abs(Decimal(str(cached.get('size', 0)))),
                entry_price=Decimal(str(cached.get('entry_price', 0))),
                mark_price=None,
                current_price=None,
                unrealized_pnl=Decimal(str(cached.get('unrealized_pnl', 0))),
                realized_pnl=Decimal('0'),
                percentage=None,
                leverage=1,
                margin_mode=MarginMode.CROSS,
                margin=Decimal('0'),
                liquidation_price=None,
                timestamp=cached.get('timestamp') or datetime.now(),
                raw_data={'source': 'ws', **cached}
            ))
        
        if cached_positions:
            if self.logger:
                self.logger.debug(f"✅ [Lighter] 使用WebSocket持仓缓存: {len(cached_positions)}个持仓")
        else:
            if self.logger:
                self.logger.debug("ℹ️ [Lighter] WebSocket持仓缓存存在但没有匹配的持仓数据")
        
        return cached_positions

    async def get_order_history(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[OrderData]:
        """
        获取历史订单（ExchangeInterface标准方法）
        """
        normalized_symbol = self._normalize_symbol(symbol) if symbol else None
        safe_limit = limit or 100
        
        async with self._rest_lock:
            orders = await self._rest.get_order_history(normalized_symbol, safe_limit)
        
        if orders:
            self.logger.debug(f"✅ [Lighter] REST获取历史订单: {len(orders)} 条")
        else:
            self.logger.debug("ℹ️ [Lighter] REST历史订单为空（可能无已完成订单或接口返回空）")
        return orders

    # ============= 交易功能 =============

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Optional[Decimal] = None,
        params: Optional[Dict[str, Any]] = None,
        batch_mode: bool = False
    ) -> OrderData:
        """
        创建订单（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            side: 订单方向（OrderSide枚举）
            order_type: 订单类型（OrderType枚举）
            amount: 数量
            price: 价格（限价单必需）
            params: 额外参数
            batch_mode: 批量模式（避免频繁查询order_index）

        Returns:
            OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)

        # 转换枚举类型为字符串
        side_str = side.value.lower()  # "buy" 或 "sell"
        order_type_str = order_type.value.lower()  # "limit" 或 "market"

        # 🔥 记录下单日志
        self.logger.info(
            f"[Lighter] 创建订单: {normalized_symbol} {side_str} {amount} @ {price or 'market'} ({order_type_str})"
        )

        # 调用内部的place_order方法，传递 batch_mode
        result = await self._rest.place_order(
            normalized_symbol, side_str, order_type_str, amount, price,
            batch_mode=batch_mode, **(params or {})
        )
        
        if result:
            self.logger.info(
                f"✅ [Lighter] 订单已提交: order_id={result.id}, status={result.status.value}"
            )
        
        return result

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        **kwargs
    ) -> Optional[OrderData]:
        """
        下单（兼容旧接口）

        Args:
            symbol: 交易对符号
            side: 订单方向 ("buy" 或 "sell")
            order_type: 订单类型 ("limit" 或 "market")
            quantity: 数量
            price: 价格（限价单必需）
            **kwargs: 其他参数

        Returns:
            OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.place_order(
            normalized_symbol, side, order_type, quantity, price, **kwargs
        )

    async def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        reduce_only: bool = False,
        skip_order_index_query: bool = False
    ) -> Optional[OrderData]:
        """
        下市价单（便捷方法）

        Args:
            symbol: 交易对符号
            side: 订单方向
            quantity: 数量
            reduce_only: 只减仓模式（平仓专用，不会开新仓或加仓）
            skip_order_index_query: 跳过 order_index 查询（Volume Maker 使用）

        Returns:
            订单数据 或 None
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.place_market_order(
            normalized_symbol, side, quantity, reduce_only, skip_order_index_query
        )

    async def place_market_orders_ws_batch(
        self,
        orders: List[Dict[str, Any]],
        *,
        slippage_multiplier: Decimal = Decimal("1.0"),
        slippage_percent: Optional[Decimal] = None
    ) -> Optional[Dict[str, Any]]:
        """
        使用WebSocket批量发送市价订单（lighter专用）
        """
        normalized_orders: List[Dict[str, Any]] = []
        for order in orders:
            payload = dict(order)
            symbol = payload.get("symbol")
            if symbol:
                payload["symbol"] = self._normalize_symbol(symbol)
            normalized_orders.append(payload)

        multiplier = slippage_multiplier
        if slippage_percent is not None:
            base_slippage = getattr(self._rest, "base_slippage", None)
            if base_slippage and base_slippage > Decimal("0"):
                try:
                    multiplier = Decimal(str(slippage_percent)) / base_slippage
                except (InvalidOperation, ZeroDivisionError):
                    multiplier = slippage_multiplier
        
        return await self._rest.place_market_orders_via_ws_batch(
            normalized_orders,
            slippage_multiplier=multiplier
        )

    async def get_order(self, order_id: str, symbol: str) -> OrderData:
        """
        获取订单信息（ExchangeInterface标准方法）

        Args:
            order_id: 订单ID
            symbol: 交易对符号

        Returns:
            OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        order = self._websocket.lookup_cached_order(order_id, normalized_symbol)
        if order:
            return order
        return await self._rest.get_order(order_id, normalized_symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> OrderData:
        """
        取消订单（ExchangeInterface标准方法）

        Args:
            order_id: 订单ID
            symbol: 交易对符号

        Returns:
            被取消的OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        success = await self._rest.cancel_order(normalized_symbol, order_id)

        if not success:
            raise Exception(f"Failed to cancel order {order_id}")

        cached = self._websocket.lookup_cached_order(order_id, normalized_symbol)
        if cached:
            cached.status = OrderStatus.CANCELED
            cached.remaining = Decimal("0")
            cached.filled = cached.filled or Decimal("0")
            cached.timestamp = datetime.now()
            return cached

        return OrderData(
            id=order_id,
            client_id=None,
            symbol=normalized_symbol,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            amount=Decimal("0"),
            price=None,
            filled=Decimal("0"),
            remaining=Decimal("0"),
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.CANCELED,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params={},
            raw_data={}
        )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        取消所有订单（ExchangeInterface标准方法）

        🔥 使用Lighter的批量取消API，比逐个取消更快速

        注意：Lighter的批量取消API会取消所有交易对的所有订单
        如果只需要取消特定交易对的订单，建议先获取该交易对的订单列表

        Args:
            symbol: 交易对符号（可选，批量取消会取消所有交易对的订单）

        Returns:
            被取消的订单列表
        """
        try:
            # 🔥 优先使用批量取消API（lighter_rest.py中已实现）
            cancelled_orders = await self._rest.cancel_all_orders(symbol)
            return cancelled_orders

        except Exception as e:
            self.logger.error(f"批量取消订单失败: {e}")
            # 降级：尝试逐个取消（已在rest层处理）
            return []

    # ============= WebSocket订阅 =============

    async def subscribe_user_data(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        订阅用户数据流（订单更新、持仓变化等）

        这是ExchangeInterface标准方法，网格系统使用此方法监控订单成交

        Args:
            callback: 数据回调函数
        """
        # Lighter的用户数据流包括订单和持仓更新
        # 我们订阅订单更新流，这是网格系统最关键的需求
        await self._websocket.subscribe_orders(callback)
        self.logger.info("✅ 已订阅Lighter用户数据流（订单更新）")

    async def subscribe_ticker(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅ticker数据

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.subscribe_ticker(normalized_symbol, callback)

    async def subscribe_orderbook(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅订单簿

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.subscribe_orderbook(normalized_symbol, callback)

    async def subscribe_trades(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅成交数据

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        self.logger.warning(
            f"⚠️ Lighter暂不支持独立的trades订阅: symbol={symbol}，请使用订单/订单簿回调"
        )

    async def subscribe_orders(self, callback: Optional[Callable] = None):
        """
        订阅订单更新

        Args:
            callback: 数据回调函数
        """
        if callback:
            self._order_callbacks.append(callback)
        await self._websocket.subscribe_orders(callback)

    async def subscribe_positions(self, callback: Optional[Callable] = None):
        """
        订阅持仓更新

        Args:
            callback: 数据回调函数
        """
        if callback:
            self._position_callbacks.append(callback)
        await self._websocket.subscribe_positions(callback)
    
    async def batch_subscribe_tickers(self, symbols: List[str], callback: Optional[Callable] = None) -> None:
        """
        批量订阅多个交易对的ticker数据（参考套利监控的订阅方式）
        
        Lighter的批量订阅策略：
        1. 第一个symbol注册回调，后续传None复用统一回调
        2. WebSocket内部会批量发送订阅消息
        
        Args:
            symbols: 交易对符号列表
            callback: 数据回调函数（只对第一个symbol注册）
        """
        if not symbols:
            self.logger.warning("批量订阅ticker: 符号列表为空")
            return
        
        self.logger.info(f"📊 开始批量订阅ticker: {len(symbols)} 个交易对")
        
        # 🔥 Lighter批量订阅策略：第一个注册回调，后续传None复用
        for idx, symbol in enumerate(symbols):
            try:
                if idx == 0:
                    # 第一个symbol：注册回调
                    await self.subscribe_ticker(symbol, callback)
                    self.logger.info(f"✅ {symbol} (首次注册统一回调)")
                else:
                    # 后续symbol：传None复用统一回调
                    await self.subscribe_ticker(symbol, None)
                    self.logger.debug(f"✅ {symbol} (复用统一回调)")
            except Exception as e:
                self.logger.error(f"❌ 批量订阅失败: {symbol} | 原因: {e}")
        
        self.logger.info(f"✅ 批量订阅完成: {len(symbols)} 个交易对")
    
    async def batch_subscribe_orderbooks(self, symbols: List[str], callback: Optional[Callable] = None) -> None:
        """
        批量订阅多个交易对的订单簿数据（参考套利监控的订阅方式）
        
        Lighter的批量订阅策略：
        1. 第一个symbol注册回调，后续传None复用统一回调
        2. WebSocket内部会批量发送订阅消息
        
        Args:
            symbols: 交易对符号列表
            callback: 数据回调函数（只对第一个symbol注册）
        """
        if not symbols:
            self.logger.warning("批量订阅订单簿: 符号列表为空")
            return
        
        self.logger.info(f"📊 开始批量订阅订单簿: {len(symbols)} 个交易对")
        
        # 🔥 Lighter批量订阅策略：第一个注册回调，后续传None复用
        for idx, symbol in enumerate(symbols):
            try:
                if idx == 0:
                    # 第一个symbol：注册回调
                    await self.subscribe_orderbook(symbol, callback)
                    self.logger.info(f"✅ {symbol} (首次注册统一回调)")
                else:
                    # 后续symbol：传None复用统一回调
                    await self.subscribe_orderbook(symbol, None)
                    self.logger.debug(f"✅ {symbol} (复用统一回调)")
            except Exception as e:
                self.logger.error(f"❌ 批量订阅订单簿失败: {symbol} | 原因: {e}")
        
        self.logger.info(f"✅ 批量订阅订单簿完成: {len(symbols)} 个交易对")

    async def unsubscribe_ticker(self, symbol: str):
        """取消订阅ticker"""
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.unsubscribe_ticker(normalized_symbol)

    async def unsubscribe_orderbook(self, symbol: str):
        """取消订阅订单簿"""
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.unsubscribe_orderbook(normalized_symbol)

    async def unsubscribe_trades(self, symbol: str):
        """取消订阅成交"""
        self.logger.debug(f"ℹ️ Lighter trades订阅已停用，symbol={symbol} 无需额外操作")

    async def unsubscribe(self, symbol: Optional[str] = None) -> None:
        """
        取消订阅（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号，None表示取消所有订阅
        """
        try:
            if symbol:
                # 取消特定符号的所有订阅
                normalized_symbol = self._normalize_symbol(symbol)
                await self._websocket.unsubscribe_ticker(normalized_symbol)
                await self._websocket.unsubscribe_orderbook(normalized_symbol)
                await self._websocket.unsubscribe_trades(normalized_symbol)
                self.logger.info(f"✅ 已取消订阅: {symbol}")
            else:
                # 取消所有订阅
                await self._websocket.disconnect()
                self.logger.info("✅ 已取消所有订阅")
        except Exception as e:
            self.logger.error(f"取消订阅失败: {e}")

    async def reconnect_websocket(self) -> None:
        """
        WebSocket重连（统一接口）

        🔥 供position_monitor等模块调用

        功能：
        1. 断开旧的WebSocket连接（SDK + 直接订阅）
        2. 等待延迟（指数退避）
        3. 重新建立连接
        4. 重新订阅所有频道（订单、持仓、市场数据）

        由内部的_websocket.reconnect()实现，该方法已包含：
        - disconnect(): 关闭所有WebSocket连接和任务
        - connect(): 重新建立连接
        - _resubscribe_all(): 重新订阅所有频道
        """
        try:
            self.logger.info("🔌 开始WebSocket重连（Lighter）...")
            await self._websocket.reconnect()
            self.logger.info("✅ WebSocket重连完成（Lighter）")
        except Exception as e:
            self.logger.error(f"❌ WebSocket重连失败（Lighter）: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            raise

    # ============= 杠杆和保证金 =============

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        设置杠杆倍数（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            leverage: 杠杆倍数

        Returns:
            Dict: 设置结果
        """
        # Lighter SDK中可能有对应的方法，这里先返回警告
        self.logger.warning("Lighter适配器暂不支持设置杠杆")
        return {
            "success": False,
            "message": "Lighter暂不支持杠杆设置"
        }

    async def set_margin_mode(self, symbol: str, margin_mode: str, leverage: int = 1) -> bool:
        """
        设置保证金模式（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            margin_mode: 保证金模式（'cross'=全仓 或 'isolated'=逐仓）
            leverage: 杠杆倍数（默认1倍）

        Returns:
            bool: 是否设置成功
        """
        # 调用REST API设置保证金模式
        return await self._rest.set_margin_mode(symbol, margin_mode, leverage)

    # ============= 辅助方法 =============

    def _normalize_symbol(self, symbol: str) -> str:
        """
        标准化交易对符号

        Args:
            symbol: 原始符号

        Returns:
            标准化后的符号
        """
        if not symbol:
            return symbol

        # 先检查是否有自定义映射
        if symbol in self._symbol_mapping:
            return self._symbol_mapping[symbol]

        # 使用base模块的标准化方法
        return self._base.normalize_symbol(symbol)

    # ------------------------------------------------------------------ #
    # 轻量重启：nonce 异常时重建 REST/WS，保留缓存与回调
    # ------------------------------------------------------------------ #
    def restart_connections(self) -> None:
        """
        局部重启适配器的 REST 和 WebSocket 连接，避免全局重启。
        - 重新创建 LighterRest / LighterWebSocket
        - 保留共享缓存和回调（持仓/订单/余额、回调列表）
        - 继承已有的 backoff_controller 引用
        """
        try:
            log = getattr(self, "logger", None) or get_logger(__name__)
            log.warning("[Lighter] 正在重建 REST/WS 连接（局部重启适配器）")
            backoff_ctrl = getattr(self, "_backoff_controller", None)

            # 重新创建
            new_rest = LighterRest(self._config_dict)
            new_ws = LighterWebSocket(self._config_dict)

            # 缓存/回调共享
            new_ws._position_cache = self._position_cache
            new_ws._order_cache = self._order_cache
            new_ws._order_cache_by_symbol = self._order_cache_by_symbol
            new_ws._balance_cache = self._balance_cache
            new_ws._position_callbacks = self._position_callbacks
            new_ws._order_callbacks = self._order_callbacks

            # REST-WS 关联
            new_rest.ws = new_ws

            # 继承 backoff_controller
            if backoff_ctrl:
                try:
                    new_rest._backoff_controller = backoff_ctrl
                except Exception:
                    pass
                try:
                    new_ws._backoff_controller = backoff_ctrl
                except Exception:
                    pass

            # 切换引用
            self._rest = new_rest
            self._websocket = new_ws

            log.info("[Lighter] REST/WS 重建完成")
        except Exception as e:
            log.error(f"[Lighter] 局部重启适配器失败: {e}", exc_info=True)

    async def get_supported_symbols(self) -> List[str]:
        """
        获取支持的交易对列表（异步方法）

        Returns:
            交易对符号列表
        """
        return self._supported_symbols.copy()

    def __repr__(self) -> str:
        """字符串表示"""
        return f"LighterAdapter(connected={self._connected}, symbols={len(self._supported_symbols)})"
