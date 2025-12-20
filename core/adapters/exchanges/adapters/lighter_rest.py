"""
Lighter交易所适配器 - REST API模块

封装Lighter SDK的REST API功能，提供市场数据、账户信息和交易功能

⚠️  Lighter交易所特殊说明：

1. Market ID从0开始（不是1）：
   - market_id=0: ETH
   - market_id=1: BTC
   - market_id=2: SOL

2. 动态价格精度（关键特性）：
   - 不同交易对使用不同的价格精度
   - 价格乘数公式: price_int = price_usd × (10 ** price_decimals)
   - 数量使用1e5: base_amount = quantity × 100000 (实际测试确认)

   示例：
   - ETH (2位小数): $4127.39 × 100 = 412739
   - BTC (1位小数): $114357.8 × 10 = 1143578
   - SOL (3位小数): $199.058 × 1000 = 199058
   - DOGE (6位小数): $0.202095 × 1000000 = 202095

3. 必须使用order_books() API：
   - 获取完整市场列表必须用order_books()
   - order_book_details(market_id) 只返回活跃市场
   - 不能通过循环遍历market_id来发现市场

这些设计是Lighter作为Layer 2 DEX的优化选择，与传统CEX不同！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔥 重要修复说明（2025-11-03）：价格精度问题（双重保险）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【修复目标】
确保 _query_order_index() 能够正确找到刚下的订单，即使传入的价格精度不正确。

【修复方案】
在 _query_order_index() 开头，对传入的 price 和 amount 做 quantize 处理：
- 获取市场信息，得到 price_decimals
- 对 price 做 quantize（与下单时相同的精度）
- 对 amount 做 quantize（避免浮点误差）

【为什么需要双重保险】
虽然 grid_config.py 已经修复了价格范围的精度问题，但这里的修复可以：
1. 防止其他地方传入错误精度的价格
2. 确保查询逻辑的健壮性
3. 作为最后一道防线，确保查询一定能成功

【影响范围】
所有交易所均受益，特别是价格移动网格模式。
"""

from ..models import (
    TickerData, OrderBookData, TradeData, BalanceData,
    OrderData, PositionData, ExchangeInfo, OrderBookLevel, OrderSide, OrderType, OrderStatus
)
from .lighter_base import LighterBase
from typing import Dict, Any, Optional, List, Callable, Tuple
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
import asyncio
import logging
import ssl
import os

from ..utils.logger_factory import get_exchange_logger

logger = get_exchange_logger("ExchangeAdapter.lighter")


class LighterReduceOnlyError(Exception):
    """Raised when Lighter rejects an order due to reduce-only restrictions."""


try:
    import lighter
    from lighter import Configuration, ApiClient, SignerClient
    from lighter.api import AccountApi, OrderApi, TransactionApi, CandlestickApi, FundingApi
    LIGHTER_AVAILABLE = True
except ImportError:
    LIGHTER_AVAILABLE = False
    logger.warning(
        "lighter SDK未安装。请执行: pip install git+https://github.com/elliottech/lighter-python.git"
    )


class LighterRest(LighterBase):
    """Lighter REST API封装类"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化Lighter REST客户端

        Args:
            config: 配置字典
        """
        if not LIGHTER_AVAILABLE:
            raise ImportError("lighter SDK未安装，无法使用Lighter适配器")

        super().__init__(config)

        # 初始化客户端
        self.api_client: Optional[ApiClient] = None
        # 🔥 signer_client 已在父类 LighterBase 中初始化，不需要重新设置为 None

        # API实例
        self.account_api: Optional[AccountApi] = None
        self.order_api: Optional[OrderApi] = None
        self.transaction_api: Optional[TransactionApi] = None
        self.candlestick_api: Optional[CandlestickApi] = None
        self.funding_api: Optional[FundingApi] = None

        # 连接状态
        self._connected = False

        # 🔥 初始化markets字典（用于WebSocket共享）
        self.markets = {}

        # 🔥 滑点配置（默认万分之2 = 0.02%）
        self.base_slippage = Decimal(str(config.get('slippage', '0.0002')))

        # 🔥 市场信息缓存（避免频繁调用API触发429限流）
        # 这是关键修复！没有这个缓存会导致批量下单时触发429
        self._market_info_cache = {}  # {symbol: {info, timestamp}}

        # 🔥 初始化WebSocket模块（用于订单成交监控）
        self._websocket = None
        try:
            from .lighter_websocket import LighterWebSocket
            self._websocket = LighterWebSocket(config)
            logger.debug("[Lighter] REST: WebSocket模块已初始化")
        except Exception as e:
            logger.warning(f"⚠️ Lighter WebSocket初始化失败: {e}")
            self._websocket = None

        # 🔥 令牌缓存（避免频繁生成令牌）
        self._auth_token_cache = None
        self._auth_token_expiry = 0  # 令牌过期时间戳

        # 错误避让控制器（与WebSocket共享）
        self._backoff_controller = None
        if self._websocket and hasattr(self._websocket, '_backoff_controller'):
            self._backoff_controller = self._websocket._backoff_controller
        else:
            try:
                from core.services.arbitrage_monitor_v2.risk_control.error_backoff_controller import ErrorBackoffController
                self._backoff_controller = ErrorBackoffController()
            except ImportError:
                logger.warning("⚠️ 错误避让控制器未初始化（模块未找到）")

        logger.info("[Lighter] REST: 初始化完成")

    @staticmethod
    def _is_reduce_only_message(message: Optional[str]) -> bool:
        if not message:
            return False
        lowered = message.lower()
        return "invalid reduce only mode" in lowered or "code=21740" in lowered
    
    def _register_backoff_error(self, error_code: str, error_message: str = ""):
        """
        注册错误到避让控制器
        
        Args:
            error_code: 错误码
            error_message: 错误信息
        """
        if self._backoff_controller:
            self._backoff_controller.register_error("lighter", error_code, error_message)

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected

    async def subscribe_user_data(self, callback: Callable) -> None:
        """
        订阅用户数据流（订单更新、持仓变化等）

        委托给WebSocket模块处理
        """
        if not self._websocket:
            logger.warning("⚠️ WebSocket模块未初始化，无法订阅用户数据")
            return

        await self._websocket.subscribe_orders(callback)
        logger.info("✅ 已订阅用户数据流（通过WebSocket）")

    async def subscribe_orderbook(self, symbol: str, callback: Callable) -> None:
        """
        订阅订单簿数据流

        Args:
            symbol: 交易对符号
            callback: 订单簿回调函数

        委托给WebSocket模块处理
        """
        if not self._websocket:
            logger.warning("⚠️ WebSocket模块未初始化，无法订阅订单簿")
            return

        await self._websocket.subscribe_orderbook(symbol, callback)
        logger.info(f"✅ 已订阅订单簿数据流: {symbol}（通过WebSocket）")

    async def subscribe_positions(self, callback: Callable) -> None:
        """
        订阅持仓数据流

        Args:
            callback: 持仓回调函数

        委托给WebSocket模块处理
        """
        if not self._websocket:
            logger.warning("⚠️ WebSocket模块未初始化，无法订阅持仓")
            return

        await self._websocket.subscribe_positions(callback)
        logger.info("✅ 已订阅持仓数据流（通过WebSocket）")

    def _get_auth_token(self) -> Optional[str]:
        """
        获取认证令牌

        ⚠️ 复刻旧版本逻辑：不使用缓存，每次直接生成。
        Lighter 服务端对 Token 时间戳校验极严，缓存容易导致 expired token。
        """
        if not self.signer_client:
            logger.error("❌ SignerClient 未初始化，无法生成认证令牌")
            return None

        try:
            import lighter
            # 使用 SDK 默认的 10 分钟有效期
            auth_token, err = self.signer_client.create_auth_token_with_expiry(
                lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY
            )

            if err:
                logger.error(f"❌ 生成认证令牌失败: {err}")
                return None

            return auth_token

        except Exception as e:
            logger.error(f"❌ 生成认证令牌异常: {e}", exc_info=True)
            return None

    async def connect(self):
        """连接（调用initialize）"""
        await self.initialize()

    async def initialize(self):
        """初始化API客户端"""
        try:
            # 🔥 SSL 配置：从环境变量读取
            verify_ssl = os.getenv('LIGHTER_VERIFY_SSL',
                                   'true').lower() == 'true'

            # 创建API客户端配置
            configuration = Configuration(host=self.base_url)
            configuration.verify_ssl = verify_ssl  # 设置 SSL 验证
            self.api_client = ApiClient(configuration=configuration)

            # 创建各种API实例
            self.account_api = AccountApi(self.api_client)
            self.order_api = OrderApi(self.api_client)
            self.transaction_api = TransactionApi(self.api_client)
            self.candlestick_api = CandlestickApi(self.api_client)
            self.funding_api = FundingApi(self.api_client)

            # 🔥 从环境变量或配置文件加载认证信息（优先使用环境变量）
            # 优先使用环境变量，如果没有则使用配置文件的值
            api_key_private_key = os.getenv(
                'LIGHTER_API_KEY_PRIVATE_KEY') or self.api_key_private_key

            # account_index: 优先环境变量，其次配置文件，最后默认None
            env_account_index = os.getenv('LIGHTER_ACCOUNT_INDEX')
            if env_account_index is not None:
                account_index = int(env_account_index)
            elif self.account_index is not None:
                account_index = self.account_index
            else:
                account_index = None

            # api_key_index: 优先环境变量，其次配置文件，最后默认None
            env_api_key_index = os.getenv('LIGHTER_API_KEY_INDEX')
            if env_api_key_index is not None:
                api_key_index = int(env_api_key_index)
            elif self.api_key_index is not None:
                api_key_index = self.api_key_index
            else:
                api_key_index = None

            # 🔥 即使auth_enabled=False，如果配置了认证信息，也初始化SignerClient用于REST API查询
            # auth_enabled只控制WebSocket是否订阅账户数据，不影响REST API的余额查询能力
            if account_index is not None and api_key_private_key:
                # 显示认证信息来源
                key_source = "环境变量" if os.getenv(
                    'LIGHTER_API_KEY_PRIVATE_KEY') else "配置文件"
                account_source = "环境变量" if os.getenv(
                    'LIGHTER_ACCOUNT_INDEX') else "配置文件"
                api_key_index_source = "环境变量" if os.getenv(
                    'LIGHTER_API_KEY_INDEX') else "配置文件"

                logger.info(
                    f"✅ Lighter 认证信息加载成功:\n"
                    f"   - API密钥: 从{key_source}加载\n"
                    f"   - account_index={account_index} (从{account_source})\n"
                    f"   - api_key_index={api_key_index} (从{api_key_index_source})"
                )

                # 更新实例属性
                self.api_key_private_key = api_key_private_key
                self.account_index = account_index
                self.api_key_index = api_key_index

                # 兼容 lighter-sdk 旧/新版本的 SignerClient
                self.signer_client = self._create_signer_client(
                    base_url=self.base_url,
                    account_index=account_index,
                    api_key_index=api_key_index,
                    api_key_private_key=api_key_private_key,
                )

                # 检查客户端
                err = self.signer_client.check_client()
                if err is not None:
                    error_msg = self.parse_error(err)
                    logger.error(f"❌ SignerClient检查失败: {error_msg}")
                    raise Exception(f"SignerClient初始化失败: {error_msg}")

                logger.info(f"✅ Lighter SignerClient 初始化成功")
            else:
                logger.warning(
                    "⚠️ [Lighter REST] 未配置认证信息（缺少 api_key_private_key 或 account_index），无法下单和查询账户数据")

            self._connected = True
            logger.info("Lighter REST客户端连接成功")

            # 加载市场信息
            await self._load_markets()

            # 🔥 连接WebSocket（如果已初始化）
            if self._websocket:
                try:
                    logger.info(
                        f"🔗 准备连接WebSocket - markets数量: {len(self.markets)}, _markets_cache数量: {len(self._markets_cache)}")

                    # 共享markets信息给WebSocket
                    self._websocket.markets = self.markets
                    self._websocket._markets_cache = getattr(
                        self, '_markets_cache', {})

                    await self._websocket.connect()
                    logger.info("✅ Lighter WebSocket已连接")
                except Exception as ws_err:
                    logger.warning(
                        f"⚠️ Lighter WebSocket连接失败: {ws_err}，将使用fallback方案")
                    import traceback
                    logger.debug(f"WebSocket错误详情:\n{traceback.format_exc()}")
            else:
                logger.warning("⚠️ WebSocket模块未初始化")

        except Exception as e:
            logger.error(f"Lighter REST客户端初始化失败: {e}")
            raise

    async def close(self):
        """关闭连接"""
        try:
            # 🔥 断开WebSocket
            if self._websocket:
                try:
                    await self._websocket.disconnect()
                except Exception as ws_err:
                    logger.warning(f"断开WebSocket时出错: {ws_err}")

            if self.signer_client:
                # 新版 SignerClient 不一定有 close；兼容检查
                close_fn = getattr(self.signer_client, "close", None)
                if close_fn:
                    await close_fn()
            if self.api_client:
                await self.api_client.close()
            self._connected = False
            logger.info("Lighter REST客户端已关闭")
        except Exception as e:
            logger.error(f"关闭Lighter REST客户端时出错: {e}")

    async def disconnect(self):
        """断开连接（调用close）"""
        await self.close()

    # ============= SDK 兼容封装 =============

    def _create_signer_client(self, base_url: str, account_index: int, api_key_index: Optional[int], api_key_private_key: str):
        """
        兼容 lighter-sdk 旧/新版本的 SignerClient 构造参数：
        - 新版(>=1.0.1): 需要 api_private_keys: Dict[int, str]
        - 旧版: 接受 private_key / api_key_index / account_index
        """
        from lighter import SignerClient
        api_key_index_val = api_key_index if api_key_index is not None else 0
        api_private_keys = {api_key_index_val: api_key_private_key}
        try:
            return SignerClient(
                url=base_url,
                account_index=account_index,
                api_private_keys=api_private_keys,
            )
        except TypeError:
            return SignerClient(
                url=base_url,
                private_key=api_key_private_key,
                account_index=account_index,
                api_key_index=api_key_index,
            )

    # ============= 市场数据 =============

    async def _load_markets(self):
        """
        加载市场信息

        ⚠️  重要说明：
        - Lighter的market_id从0开始，不是从1开始
        - ETH的market_id是0（这是最重要的市场）
        - 必须使用order_books() API获取完整列表，不能用循环遍历market_id
        - order_book_details(market_id) 只返回有交易的市场，可能漏掉不活跃的市场

        市场ID示例：
        - market_id=0: ETH (价格精度: 2位小数, 乘数: 100)
        - market_id=1: BTC (价格精度: 1位小数, 乘数: 10)
        - market_id=2: SOL (价格精度: 3位小数, 乘数: 1000)
        """
        try:
            # 获取订单簿列表（包含市场信息）
            # ⚠️ 必须使用此API，它会返回所有市场包括market_id=0的ETH
            response = await self.order_api.order_books()

            if hasattr(response, 'order_books'):
                markets = []
                for order_book_info in response.order_books:
                    if hasattr(order_book_info, 'symbol') and hasattr(order_book_info, 'market_id'):
                        # 🔥 获取 Lighter API 返回的原始符号格式
                        raw_symbol = order_book_info.symbol

                        market_info = {
                            "market_id": order_book_info.market_id,  # 使用API返回的真实 market_id
                            "symbol": raw_symbol,  # 保持 Lighter API 原始符号格式
                        }
                        markets.append(market_info)

                        # 🔥 同时填充 self.markets 字典（用于WebSocket）
                        self.markets[raw_symbol] = market_info

                        # 🔥 记录前3个市场的符号格式用于诊断
                        if len(markets) <= 3:
                            logger.info(
                                f"📊 市场 {len(markets)}: symbol={raw_symbol}, market_id={order_book_info.market_id}")

                self.update_markets_cache(markets)
                logger.info(f"加载了 {len(markets)} 个市场")
                # 🔥 输出缓存的符号列表（前5个）
                cached_symbols = list(self._symbol_to_market_index.keys())[:5]
                logger.info(f"🔍 缓存的符号示例: {cached_symbols}")

        except Exception as e:
            logger.error(f"加载市场信息失败: {e}")

    async def get_exchange_info(self) -> ExchangeInfo:
        """
        获取交易所信息

        Returns:
            ExchangeInfo对象
        """
        try:
            # 获取订单簿信息
            response = await self.order_api.order_books()

            symbols = []
            if hasattr(response, 'order_books'):
                for ob in response.order_books:
                    if hasattr(ob, 'symbol') and hasattr(ob, 'market_id'):
                        # 🔥 关键修复：提取price_decimals，确保WebSocket解析时能正确获取
                        # 从order_book对象中提取price_decimals（如果有的话）
                        price_decimals = self._extract_price_decimals(ob)

                        # 🔥 计算价格乘数（仅当有精度时）
                        # 不设默认值，避免对不同市场使用错误精度
                        # 使用时应从配置或专门API获取真实精度
                        price_multiplier = None
                        if price_decimals is not None:
                            price_multiplier = Decimal(10 ** price_decimals)

                        # 保持原始符号格式，在监控层统一处理
                        symbols.append({
                            "symbol": ob.symbol,
                            "market_id": ob.market_id,  # 使用 market_id 而不是 index
                            "market_index": ob.market_id,  # 🔥 添加market_index别名，与缓存键一致
                            "base_asset": ob.symbol.split('-')[0] if '-' in ob.symbol else "",
                            "quote_asset": ob.symbol.split('-')[1] if '-' in ob.symbol else "USD",
                            "status": getattr(ob, 'status', 'trading'),
                            "price_decimals": price_decimals,  # 可能为 None，使用时需处理
                            "price_multiplier": price_multiplier,  # 可能为 None，使用时需处理
                        })

            # 创建 ExchangeInfo 对象
            info = ExchangeInfo(
                name="Lighter",
                id="lighter",
                type=None,
                supported_features=[],
                rate_limits={},
                precision={},
                fees={},
                markets={s['symbol']: s for s in symbols},
                status="online",
                timestamp=datetime.now()
            )
            return info

        except Exception as e:
            logger.error(f"获取交易所信息失败: {e}")
            raise

    async def get_ticker(self, symbol: str) -> Optional[TickerData]:
        """
        获取ticker数据

        Args:
            symbol: 交易对符号

        Returns:
            TickerData对象
        """
        try:
            market_id = self.get_market_index(symbol)
            if market_id is None:
                logger.warning(f"未找到交易对 {symbol} 的市场ID")
                # 尝试直接返回空Ticker，避免上层异常
                return None

            # 获取市场统计信息（包含价格信息）
            response = await self.order_api.order_book_details(market_id=market_id)

            detail = None
            if response and hasattr(response, 'order_book_details') and response.order_book_details:
                detail = response.order_book_details[0]

            # 解析ticker数据（基于实际API返回字段）
            # 🔥 修复：不使用0作为默认值，避免返回无效价格（多交易所兼容性）
            last_price = self._safe_decimal(
                getattr(detail, 'last_trade_price', None)) if detail else None
            daily_high = self._safe_decimal(
                getattr(detail, 'daily_price_high', None)) if detail else None
            daily_low = self._safe_decimal(
                getattr(detail, 'daily_price_low', None)) if detail else None
            daily_volume = self._safe_decimal(
                getattr(detail, 'daily_base_token_volume', None)) if detail else None

            # 尝试获取最佳买卖价（从订单簿）
            bid_price = last_price
            ask_price = last_price
            try:
                orderbook_response = await self.order_api.order_book_orders(
                    market_id=market_id, limit=1)
                if orderbook_response and getattr(orderbook_response, 'bids', None):
                    bid_price = self._safe_decimal(
                        orderbook_response.bids[0].price)
                if orderbook_response and getattr(orderbook_response, 'asks', None):
                    ask_price = self._safe_decimal(
                        orderbook_response.asks[0].price)
            except Exception as e:
                logger.debug(f"无法获取订单簿最佳价格: {e}")

            # 如果 last / bid / ask 全部为空，放弃返回None，让上层降级处理
            if last_price is None and bid_price is None and ask_price is None:
                return None

            return TickerData(
                symbol=symbol,
                last=last_price,
                bid=bid_price,
                ask=ask_price,
                volume=daily_volume,
                high=daily_high,
                low=daily_low,
                timestamp=datetime.now()
            )

        except Exception as e:
            logger.error(f"获取ticker失败 {symbol}: {e}")
            return None

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Optional[OrderBookData]:
        """
        获取订单簿

        Args:
            symbol: 交易对符号
            limit: 深度限制

        Returns:
            OrderBookData对象
        """
        try:
            market_id = self.get_market_index(symbol)
            if market_id is None:
                logger.warning(f"未找到交易对 {symbol} 的市场ID")
                return None

            # 使用 order_book_orders 获取订单簿深度
            response = await self.order_api.order_book_orders(market_id=market_id, limit=limit)

            if not response:
                return None

            # 解析买单和卖单
            # Lighter返回完整订单对象：{price, remaining_base_amount, order_id, ...}
            bids = []
            asks = []

            if hasattr(response, 'bids') and response.bids:
                for bid in response.bids[:limit]:
                    # 提取 price 和 remaining_base_amount
                    price = self._safe_decimal(getattr(bid, 'price', 0))
                    quantity = self._safe_decimal(
                        getattr(bid, 'remaining_base_amount', 0))

                    if price > 0 and quantity > 0:
                        bids.append(OrderBookLevel(
                            price=price,
                            size=quantity
                        ))

            if hasattr(response, 'asks') and response.asks:
                for ask in response.asks[:limit]:
                    # 提取 price 和 remaining_base_amount
                    price = self._safe_decimal(getattr(ask, 'price', 0))
                    quantity = self._safe_decimal(
                        getattr(ask, 'remaining_base_amount', 0))

                    if price > 0 and quantity > 0:
                        asks.append(OrderBookLevel(
                            price=price,
                            size=quantity
                        ))

            return OrderBookData(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(),
                nonce=None
            )

        except Exception as e:
            error_str = str(e)
            # 🔥 检测 429 限流错误
            if "429" in error_str or "Too Many Requests" in error_str:
                self._register_backoff_error("429", error_str)
            logger.error(f"获取订单簿失败 {symbol}: {e}")
            return None

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[TradeData]:
        """
        获取最近成交

        Args:
            symbol: 交易对符号
            limit: 数量限制

        Returns:
            TradeData列表
        """
        try:
            market_id = self.get_market_index(symbol)
            if market_id is None:
                logger.warning(f"未找到交易对 {symbol} 的市场ID")
                return []

            # 获取最近成交
            response = await self.order_api.recent_trades(market_id=market_id, limit=limit)

            trades = []
            if hasattr(response, 'trades') and response.trades:
                for trade in response.trades:
                    price = self._safe_decimal(trade.price) if hasattr(
                        trade, 'price') else Decimal("0")
                    amount = self._safe_decimal(trade.size) if hasattr(
                        trade, 'size') else Decimal("0")
                    cost = price * amount

                    # 解析成交方向
                    is_ask = getattr(trade, 'is_ask', False)
                    side = OrderSide.SELL if is_ask else OrderSide.BUY

                    trades.append(TradeData(
                        id=str(getattr(trade, 'trade_id', '')),
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        price=price,
                        cost=cost,
                        fee=None,
                        timestamp=self._parse_timestamp(
                            getattr(trade, 'timestamp', None)) or datetime.now(),
                        order_id=str(getattr(trade, 'order_id', '')),
                        raw_data={'trade': trade}
                    ))

            return trades

        except Exception as e:
            logger.error(f"获取最近成交失败 {symbol}: {e}")
            return []

    # ============= 账户信息 =============

    async def get_account_balance(self) -> List[BalanceData]:
        """
        获取账户余额

        Returns:
            BalanceData列表
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法获取账户信息")
            return []

        try:
            # 获取账户信息
            response = await self.account_api.account(by="index", value=str(self.account_index))

            balances = []

            # 解析 DetailedAccounts 结构
            if hasattr(response, 'accounts') and response.accounts:
                # 获取第一个账户（通常就是查询的账户）
                account = response.accounts[0]

                # 获取可用余额和抵押品（USDC余额）
                available_balance = self._safe_decimal(
                    getattr(account, 'available_balance', 0))
                collateral = self._safe_decimal(
                    getattr(account, 'collateral', 0))

                # 计算锁定余额（抵押品 - 可用余额）
                locked = max(collateral - available_balance, Decimal("0"))

                # USDC 余额（Lighter是合约交易所，只有USDC保证金）
                if collateral > 0:
                    from datetime import datetime
                    balances.append(BalanceData(
                        currency="USDC",
                        free=available_balance,
                        used=locked,
                        total=collateral,
                        usd_value=collateral,
                        timestamp=datetime.now(),
                        raw_data={'source': 'rest',
                                  'account': account}  # 🔥 标记来源
                    ))

                # 注意：Lighter是合约交易所，持仓不是余额
                # 持仓应该通过 get_positions() 方法查询

            return balances

        except Exception as e:
            error_str = str(e)
            # 🔥 优化：429错误使用WARNING级别，并简化日志格式
            if "429" in error_str or "Too Many Requests" in error_str:
                logger.warning(f"[Lighter] API限流 (429)，余额查询失败")
            else:
                logger.error(f"[Lighter] 余额查询失败: {error_str}")
            return []

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        获取活跃订单

        Args:
            symbol: 交易对符号（可选，为None时获取所有）

        Returns:
            OrderData列表
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法获取订单信息")
            return []

        try:
            # 🔥 修复：Lighter 需要使用专门的订单查询 API，而不是 account API
            # 获取 market_id
            market_id = None
            if symbol:
                market_id = self.get_market_index(symbol)
                if market_id is None:
                    logger.warning(f"未找到交易对 {symbol} 的市场索引")
                    return []

            # 🔥 重试机制：如果令牌过期，重新生成
            max_retries = 3
            for attempt in range(max_retries):
                # 获取认证令牌（使用缓存机制）
                auth_token = self._get_auth_token()
                if not auth_token:
                    logger.error(f"获取认证令牌失败")
                    return []

                try:
                    # 使用 account_active_orders API（SDK 方法是异步的，直接 await）
                    response = await self.order_api.account_active_orders(
                        account_index=self.account_index,
                        market_id=market_id if market_id is not None else 255,  # 255 = 所有市场
                        auth=auth_token
                    )
                    break  # 成功，退出重试循环

                except Exception as e:
                    error_str = str(e)
                    # 如果是令牌/认证错误，清除缓存并重试
                    if any(
                        key in error_str.lower()
                        for key in ("expired token", "20013", "invalid auth", "invalid deadline")
                    ):
                        logger.warning(
                            f"⚠️ 令牌可能失效（尝试 {attempt + 1}/{max_retries}）：{error_str}，清除缓存并重试"
                        )
                        self._auth_token_cache = None  # 🔥 清除缓存，强制重新生成
                        if attempt < max_retries - 1:
                            backoff = 0.25 * (attempt + 1)
                            await asyncio.sleep(backoff)
                            continue
                    # 其他错误直接抛出
                    raise

            orders = []

            # 🔥 account_active_orders 返回 orders 列表，不是 accounts
            if hasattr(response, 'orders') and response.orders:
                logger.info(f"🔍 REST API返回 {len(response.orders)} 个活跃订单")

                for order_info in response.orders:
                    order_symbol = self._get_symbol_from_market_index(
                        getattr(order_info, 'market_index', None))

                    # 如果指定了symbol，过滤
                    if symbol and order_symbol != symbol:
                        continue

                    orders.append(self._parse_order(order_info, order_symbol))
            else:
                logger.info(f"✅ REST API确认无活跃订单")

            return orders

        except Exception as e:
            logger.error(f"获取活跃订单失败: {e}")
            return []

    async def get_order(self, order_id: str, symbol: str) -> OrderData:
        """
        获取单个订单信息（支持部分成交识别）

        Args:
            order_id: 订单ID
            symbol: 交易对符号

        Returns:
            OrderData对象

        Raises:
            Exception: 如果订单不存在或查询失败
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法获取订单信息")
            raise Exception("未配置SignerClient")

        target_id = str(order_id)

        def _match_order(orders: List[OrderData]) -> Optional[OrderData]:
            for order in orders:
                if not order:
                    continue
                if str(order.id) == target_id or (order.client_id and str(order.client_id) == target_id):
                    return order
            return None

        try:
            open_orders = await self.get_open_orders(symbol)
            matched = _match_order(open_orders)
            if matched:
                logger.debug(f"找到活跃订单: {order_id}, 状态={matched.status.value}")
                return matched

            # 如果活跃列表未找到，再查询历史订单
            history_orders = await self.get_order_history(symbol, limit=200)
            matched = _match_order(history_orders)
            if matched:
                logger.debug(
                    f"从历史订单获取 {order_id}: 状态={matched.status.value}, filled={matched.filled}"
                )
                return matched

            logger.warning(
                f"⚠️ Lighter未找到订单 {order_id} 的详细信息（可能尚未同步或已被清理），返回占位数据"
            )

            return OrderData(
                id=order_id,
                client_id=None,
                symbol=symbol,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                amount=Decimal("0"),
                price=None,
                filled=Decimal("0"),
                remaining=Decimal("0"),
                cost=Decimal("0"),
                average=None,
                status=OrderStatus.FILLED,  # 🔥 旧版本逻辑：找不到就假设已成交
                timestamp=datetime.now(),
                updated=None,
                fee=None,
                trades=[],
                params={},
                raw_data={"fallback": True}
            )

        except Exception as e:
            logger.error(f"获取订单 {order_id} 失败: {e}")
            raise

    async def get_order_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[OrderData]:
        """
        获取历史订单（已完成/取消）

        Args:
            symbol: 交易对符号（可选）
            limit: 返回数量限制

        Returns:
            OrderData列表
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法获取订单历史")
            return []

        try:
            # 获取认证令牌（使用缓存机制）
            auth_token = self._get_auth_token()
            if not auth_token:
                logger.error(f"获取认证令牌失败")
                return []

            # 获取市场ID（如果指定了symbol）
            market_id = None
            if symbol:
                market_id = self.get_market_index(symbol)

            # 获取历史订单 (Lighter API 限制 limit <= 100)
            safe_limit = min(limit, 100)
            response = await self.order_api.account_inactive_orders(
                account_index=self.account_index,
                limit=safe_limit,
                auth=auth_token,
                market_id=market_id if market_id is not None else 255  # 255表示所有市场
            )

            orders = []
            if hasattr(response, 'orders') and response.orders:
                for order_info in response.orders:
                    order_symbol = self._get_symbol_from_market_index(
                        getattr(order_info, 'market_index', None))

                    orders.append(self._parse_order(order_info, order_symbol))

            return orders

        except Exception as e:
            # ⚠️ Lighter 有时会返回 20013 invalid auth / expired token
            # 这通常表示当前环境下 REST 历史订单接口不可用（但 WebSocket 仍然正常）
            error_str = str(e)
            if any(
                key in error_str.lower()
                for key in ("expired token", "20013", "invalid auth", "invalid deadline")
            ):
                logger.error(f"获取历史订单失败（认证问题，已忽略，改用WebSocket缓存）: {e}")
            else:
                logger.error(f"获取历史订单失败: {e}", exc_info=True)
            return []

    async def get_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:
        """
        获取持仓信息

        Args:
            symbols: 交易对符号列表（Lighter会忽略，返回所有持仓）

        Returns:
            PositionData列表
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法获取持仓信息")
            return []

        # 🔥 429错误重试机制：最多重试2次，使用指数退避
        max_retries = 2
        retry_delays = [2, 5]  # 第1次等2秒，第2次等5秒

        for attempt in range(max_retries + 1):
            try:
                # 获取账户信息（包含持仓）
                response = await self.account_api.account(by="index", value=str(self.account_index))
                # 成功获取，跳出重试循环
                break
            except Exception as e:
                error_str = str(e)
                # 检测429错误
                if "429" in error_str or "Too Many Requests" in error_str:
                    if attempt < max_retries:
                        wait_time = retry_delays[attempt]
                        logger.warning(
                            f"[Lighter] API限流 (429)，{wait_time}秒后重试 ({attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"[Lighter] 持仓查询失败: 达到最大重试次数，限流持续")
                        return []
                else:
                    # 非429错误，直接抛出
                    raise

        # 继续原有的持仓解析逻辑
        positions = []
        # 解析 DetailedAccounts 结构
        if hasattr(response, 'accounts') and response.accounts:
            account = response.accounts[0]

            if hasattr(account, 'positions') and account.positions:

                for idx, position_info in enumerate(account.positions):
                    symbol = position_info.symbol if hasattr(
                        position_info, 'symbol') else ""

                    # 获取持仓数据
                    position_raw = position_info.position if hasattr(
                        position_info, 'position') else 0

                    position_size = self._safe_decimal(position_raw) if hasattr(
                        position_info, 'position') else Decimal("0")

                    if position_size == 0:
                        continue

                    from datetime import datetime
                    from ..models import PositionSide, MarginMode

                    # 🔥 Lighter持仓方向定义（与传统CEX一致）
                    # 正数 = 多头 (LONG) | 负数 = 空头 (SHORT)
                    # ✅ 测试验证：BUY订单成交后，position返回正数，表示做多
                    position_side = PositionSide.LONG if position_size > 0 else PositionSide.SHORT

                    positions.append(PositionData(
                        symbol=symbol,
                        side=position_side,
                        size=abs(position_size),
                        entry_price=self._safe_decimal(
                            getattr(position_info, 'avg_entry_price', 0)),
                        mark_price=None,  # Lighter不提供标记价格
                        current_price=None,  # 需要单独查询
                        unrealized_pnl=self._safe_decimal(
                            getattr(position_info, 'unrealized_pnl', 0)),
                        realized_pnl=self._safe_decimal(
                            getattr(position_info, 'realized_pnl', 0)),
                        percentage=None,  # 可以计算
                        leverage=int(
                            getattr(position_info, 'leverage', 1)),
                        margin_mode=MarginMode.CROSS if getattr(
                            position_info, 'margin_mode', 0) == 0 else MarginMode.ISOLATED,
                        margin=self._safe_decimal(
                            getattr(position_info, 'allocated_margin', 0)),
                        liquidation_price=self._safe_decimal(getattr(position_info, 'liquidation_price', 0)) if getattr(
                            position_info, 'liquidation_price', '0') != '0' else None,
                        timestamp=datetime.now(),
                        raw_data={'position_info': position_info}
                    ))

        # 🔥 如果指定了symbols，只返回匹配的持仓
        if symbols:
            positions = [p for p in positions if p.symbol in symbols]

        return positions

    # ============= 交易功能 =============

    def _validate_order_preconditions(self) -> bool:
        """验证下单前置条件"""
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug(
                "未配置SignerClient，无法下单（请检查lighter_config.yaml中的api_key_private_key和account_index）")
            return False
        return True

    def _extract_price_decimals(self, market_details) -> Optional[int]:
        """从市场详情/摘要中提取价格精度，尽量容错避免缩放错误"""

        def _safe_int(val):
            try:
                return int(val)
            except Exception:
                return None

        # 1) 详细字段（order_book_details）
        if hasattr(market_details, 'order_book_details') and market_details.order_book_details:
            detail = market_details.order_book_details[0]
            for attr in ("price_decimals", "priceDecimals", "price_scale", "price_scale_decimals"):
                if hasattr(detail, attr):
                    parsed = _safe_int(getattr(detail, attr))
                    if parsed is not None:
                        return parsed

        # 2) 摘要字段（order_books / 其他对象）
        for attr in ("price_decimals", "priceDecimals", "price_scale", "price_scale_decimals"):
            if hasattr(market_details, attr):
                parsed = _safe_int(getattr(market_details, attr))
                if parsed is not None:
                    return parsed

        # 3) 兜底：返回 None，由调用方决定进一步降级
        return None

    async def _get_market_info(self, symbol: str) -> Optional[Dict]:
        """
        获取市场信息（索引、价格精度、乘数）

        🔥 使用缓存机制避免频繁API调用触发429限流
        这是关键修复！批量下单时必须使用缓存

        Returns:
            包含 market_index, price_decimals, price_multiplier 的字典，或 None
        """
        import time

        # 🔥 检查缓存（5分钟有效期）
        # 注意：缓存的是市场的静态配置（市场索引、价格精度），不是动态数据
        # 这些配置基本不会变化，所以可以缓存较长时间
        if symbol in self._market_info_cache:
            cache_entry = self._market_info_cache[symbol]
            cache_age = time.time() - cache_entry['timestamp']
            if cache_age < 300:  # 缓存5分钟内有效（300秒）
                logger.debug(f"✅ 使用缓存的市场信息: {symbol} (缓存年龄: {cache_age:.1f}秒)")
                return cache_entry['info']

        try:
            normalized_symbol = self.normalize_symbol(symbol)
            candidate_symbols = [normalized_symbol]
            base_symbol = normalized_symbol.split(
                '-')[0] if '-' in normalized_symbol else normalized_symbol
            candidate_symbols.extend([
                base_symbol,
                f"{base_symbol}-USD",
                f"{base_symbol}/USD"
            ])
            candidate_symbols.append(
                normalized_symbol.replace('-USDC-PERP', '-USD'))
            candidate_symbols.append(
                normalized_symbol.replace('-USDC-PERP', ''))
            # 去重并过滤空字符串
            seen = set()
            candidates = []
            for candidate in candidate_symbols:
                cand = candidate.strip()
                if not cand or cand in seen:
                    continue
                seen.add(cand)
                candidates.append(cand)

            market_index = None
            resolved_symbol = normalized_symbol
            for candidate in candidates:
                market_index = self.get_market_index(candidate)
                if market_index is not None:
                    resolved_symbol = candidate
                    if candidate != normalized_symbol:
                        logger.info(
                            f"🔁 [Lighter] 市场符号映射: {symbol} → {candidate} (自动匹配市场索引)")
                        # 缓存映射，避免下次重复匹配
                        self._symbol_mapping[symbol] = candidate
                    break

            logger.debug(f"✅ 获取market_index: {market_index}")
            if market_index is None:
                logger.error(f"❌ 未找到交易对 {symbol} 的市场索引")
                logger.error(
                    f"   可用市场: {list(self.markets.keys()) if self.markets else '未加载'}")
                return None

            # 获取市场详情，动态获取价格精度
            logger.debug(f"🔍 获取市场详情: market_id={market_index}")
            market_details = await self.order_api.order_book_details(market_id=market_index)
            price_decimals = self._extract_price_decimals(market_details)

            # 如果详情未提供或解析失败，尝试从已加载的 markets 字典兜底（含 price_decimals）
            if price_decimals is None and self.markets:
                market_entry = self.markets.get(resolved_symbol)
                if market_entry is None:
                    # 兼容原始符号可能是 normalized_symbol
                    market_entry = self.markets.get(normalized_symbol)
                if market_entry and 'price_decimals' in market_entry:
                    price_decimals = market_entry['price_decimals']
                    logger.debug(
                        f"🔄 使用缓存市场精度兜底: price_decimals={price_decimals}")

            # 再兜底：仍为空则使用 2（现货 ETH/USDC 官方精度为 2），避免价格缩放错误
            if price_decimals is None:
                price_decimals = 2
                logger.warning(
                    "⚠️ 未能从市场详情获取 price_decimals，使用兜底值 2（现货默认），避免价格被缩小10倍")

            logger.debug(f"✅ 获取价格精度成功: price_decimals={price_decimals}")

            price_multiplier = Decimal(10 ** price_decimals)
            logger.debug(
                f"{symbol} 价格精度: {price_decimals}位小数, 乘数: {price_multiplier}")

            market_info = {
                'market_index': market_index,
                'price_decimals': price_decimals,
                'price_multiplier': price_multiplier
            }

            # 🔥 缓存市场信息（关键！）
            cache_entry = {
                'info': market_info,
                'timestamp': time.time()
            }
            self._market_info_cache[symbol] = cache_entry
            if resolved_symbol != symbol:
                self._market_info_cache[resolved_symbol] = cache_entry
            logger.debug(f"💾 已缓存市场信息: {symbol} (解析符号: {resolved_symbol})")

            return market_info

        except Exception as e:
            logger.error(f"获取市场信息失败: {e}")
            return None

    async def _calculate_slippage_protection_price(
        self,
        symbol: str,
        side: str,
        provided_price: Optional[Decimal] = None,
        slippage_multiplier: Decimal = Decimal("1.0")
    ) -> Optional[Decimal]:
        """
        计算市价单的滑点保护价格（支持动态滑点倍数）

        Args:
            symbol: 交易对符号
            side: 订单方向
            provided_price: 决策引擎提供的目标价格（如果有）
            slippage_multiplier: 滑点倍数（1.0=0.01%, 10.0=0.1%, 100.0=1%）

        Returns:
            滑点保护价格，或 None
        """
        # 🔥 计算基础滑点比例
        base_slippage = self.base_slippage * slippage_multiplier
        is_sell = (side.lower() == "sell")

        # 🔥 如果提供了目标价格，在目标价格上应用滑点
        if provided_price and provided_price > Decimal("0"):
            if is_sell:
                # 卖单：允许价格向下浮动（容忍价格稍微低一点）
                protection_price = provided_price * \
                    (Decimal("1") - base_slippage)
            else:
                # 买单：允许价格向上浮动（容忍价格稍微高一点）
                protection_price = provided_price * \
                    (Decimal("1") + base_slippage)

            slippage_percent = base_slippage * 100
            direction = "向下" if is_sell else "向上"
            logger.info(
                f"💰 [Lighter] 市价单滑点保护 | 价格基准: 📌决策引擎信号价 | "
                f"目标价={provided_price}, 滑点={slippage_percent:.4f}%({direction}浮动), "
                f"保护价={protection_price} | {side.upper()}"
            )
            return protection_price

        # 🔥 如果没有提供目标价格，使用实时盘口价 + 滑点
        try:
            # 优先使用WebSocket缓存的订单簿（避免REST API延迟）
            orderbook = None
            if hasattr(self, 'ws') and self.ws and hasattr(self.ws, 'get_cached_orderbook'):
                orderbook = self.ws.get_cached_orderbook(symbol)
                if orderbook and orderbook.bids and orderbook.asks:
                    logger.debug(f"✅ 使用WebSocket缓存订单簿: {symbol}")

            # 如果缓存不可用，回退到REST API
            if not orderbook or not orderbook.bids or not orderbook.asks:
                logger.debug(f"⚠️ WebSocket缓存不可用，使用REST API查询订单簿: {symbol}")
                orderbook = await self.get_orderbook(symbol)
                if not orderbook or not orderbook.bids or not orderbook.asks:
                    logger.error(f"无法获取{symbol}的订单簿，市价单需要价格")
                    return None

            if is_sell:
                # 卖单：使用买1价格作为基准，允许价格向下浮动
                base_price = orderbook.bids[0].price
                protection_price = base_price * (Decimal("1") - base_slippage)
                price_label = "买一价"
            else:
                # 买单：使用卖1价格作为基准，允许价格向上浮动
                base_price = orderbook.asks[0].price
                protection_price = base_price * (Decimal("1") + base_slippage)
                price_label = "卖一价"

            slippage_percent = base_slippage * 100
            direction = "向下" if is_sell else "向上"
            logger.info(
                f"💰 [Lighter] 市价单滑点保护 | 价格基准: 🔄实时市场盘口价 | "
                f"{price_label}={base_price}, 滑点={slippage_percent:.4f}%({direction}浮动), "
                f"保护价={protection_price} | {side.upper()}"
            )
            return protection_price

        except Exception as e:
            logger.error(f"计算滑点保护价格失败: {e}")
            return None

    def _convert_market_order_params(
        self,
        market_info: Dict,
        quantity: Decimal,
        avg_execution_price: Decimal,
        side: str,
        **kwargs
    ) -> Dict:
        """转换市价单参数为Lighter格式"""
        # 🔥 先对价格应用精度规则（与限价单保持一致）
        price_decimals = market_info['price_decimals']
        if price_decimals == 0:
            quantize_precision = Decimal("1")
        else:
            quantize_precision = Decimal(10) ** (-price_decimals)

        avg_execution_price_rounded = avg_execution_price.quantize(
            quantize_precision)

        quantity_multiplier_exponent = max(0, 6 - price_decimals)
        quantity_multiplier = Decimal(10) ** quantity_multiplier_exponent
        base_amount = quantity * quantity_multiplier
        if base_amount < 1:
            base_amount = Decimal("1")
        try:
            base_amount_int = int(
                base_amount.to_integral_value(rounding=ROUND_DOWN))
        except Exception:
            base_amount_int = int(base_amount)
        if base_amount_int <= 0:
            logger.warning(
                f"⚠️ [Lighter] 下单数量过小，已提升至最小单位: 原始={quantity}, 乘数={quantity_multiplier}"
            )
            base_amount_int = 1
        avg_price_int = int(avg_execution_price_rounded *
                            market_info['price_multiplier'])
        is_ask = (side.lower() == "sell")

        logger.debug(f"  symbol参数中的market_index={market_info['market_index']}")
        logger.debug(
            f"  价格精度: {market_info['price_decimals']}位小数, 乘数: {market_info['price_multiplier']}")
        logger.debug(
            f"  avg_execution_price={avg_execution_price} -> 四舍五入后={avg_execution_price_rounded}, avg_price_int={avg_price_int}")
        logger.debug(
            f"  reduce_only={kwargs.get('reduce_only', False)}")

        # 🔥 处理 client_order_id（支持字符串和整数，向后兼容）
        client_order_id = kwargs.get("client_order_id")
        if client_order_id is not None:
            # 如果是字符串，转换为整数
            if isinstance(client_order_id, str):
                try:
                    client_order_id = int(client_order_id)
                except ValueError:
                    logger.warning(
                        f"⚠️ client_order_id 不是有效整数: {client_order_id}，使用自动生成")
                    client_order_id = int(
                        asyncio.get_event_loop().time() * 1000)
        else:
            # 如果未提供，生成默认值
            client_order_id = int(asyncio.get_event_loop().time() * 1000)

        # 🔥 注意：Lighter SDK 的 create_market_order 不接受 margin_mode 参数
        # SDK 签名: create_market_order(market_index, client_order_index, base_amount,
        #                               avg_execution_price, is_ask, reduce_only)

        return {
            'market_index': market_info['market_index'],
            'client_order_index': client_order_id,
            'base_amount': base_amount_int,
            'avg_execution_price': avg_price_int,
            'is_ask': is_ask,
            'reduce_only': kwargs.get("reduce_only", False)
        }

    def _convert_limit_order_params(
        self,
        market_info: Dict,
        quantity: Decimal,
        price: Decimal,
        side: str,
        **kwargs
    ) -> Dict:
        """转换限价单参数为Lighter格式"""
        import lighter

        # 🔥 根据price_decimals动态调整价格精度（直接使用quantize避免浮点误差）
        # 例如：price_decimals=1 -> quantize(Decimal("0.1"))
        #      price_decimals=2 -> quantize(Decimal("0.01"))
        price_decimals = market_info['price_decimals']
        if price_decimals == 0:
            quantize_precision = Decimal("1")
        else:
            quantize_precision = Decimal(10) ** (-price_decimals)

        price_rounded = price.quantize(quantize_precision)

        quantity_multiplier_exponent = max(0, 6 - price_decimals)
        quantity_multiplier = Decimal(10) ** quantity_multiplier_exponent
        base_amount = quantity * quantity_multiplier
        if base_amount < 1:
            base_amount = Decimal("1")
        try:
            base_amount_int = int(
                base_amount.to_integral_value(rounding=ROUND_DOWN))
        except Exception:
            base_amount_int = int(base_amount)
        if base_amount_int <= 0:
            logger.warning(
                f"⚠️ [Lighter] 下单数量过小，已提升至最小单位: 原始={quantity}, 乘数={quantity_multiplier}"
            )
            base_amount_int = 1
        price_int = int(price_rounded * market_info['price_multiplier'])
        is_ask = (side.lower() == "sell")

        # 🔍 简化日志：只在 DEBUG 级别输出详细参数
        logger.debug(f"Lighter限价单参数: market_id={market_info['market_index']}, "
                     f"price={price_rounded}, quantity={quantity}, "
                     f"base_amount={base_amount_int}, is_ask={is_ask}")

        time_in_force = kwargs.get("time_in_force", "GTT")
        tif_map = {
            "IOC": lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            "GTT": lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            "POST_ONLY": lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
        }

        # 🔥 注意：Lighter SDK 的 create_order 不接受 margin_mode 参数
        # margin_mode 需要在账户级别设置，不是在订单级别
        # SDK 签名: create_order(market_index, client_order_index, base_amount, price,
        #                        is_ask, order_type, time_in_force, reduce_only, trigger_price)

        return {
            'market_index': market_info['market_index'],
            'client_order_index': kwargs.get("client_order_id",
                                             int(asyncio.get_event_loop().time() * 1000)),
            'base_amount': base_amount_int,
            'price': price_int,
            'is_ask': is_ask,
            'order_type': lighter.SignerClient.ORDER_TYPE_LIMIT,
            'time_in_force': tif_map.get(time_in_force,
                                         lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME),
            'reduce_only': kwargs.get("reduce_only", False),
            'trigger_price': 0
        }

    def _get_cached_position_entry(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从WebSocket缓存中获取指定交易对的实时持仓信息
        """
        websocket = getattr(self, "_websocket", None)
        if not websocket:
            return None
        position_cache = getattr(websocket, "_position_cache", None)
        if not position_cache:
            return None
        return position_cache.get(symbol)

    def _prepare_reduce_only_quantity(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        reduce_only: bool
    ) -> Dict[str, Any]:
        """
        根据实时持仓缓存，调整只减仓订单的数量，避免超过可平仓数量
        """
        result = {
            "quantity": quantity,
            "skip": False,
            "reason": None,
            "available": None,
        }

        if not reduce_only:
            return result

        position_entry = self._get_cached_position_entry(symbol)
        if not position_entry:
            return result

        try:
            signed_size = Decimal(str(position_entry.get("size", "0")))
        except (ArithmeticError, ValueError, TypeError):
            signed_size = Decimal("0")

        normalized_side = (side or "").lower()
        if normalized_side == "sell":
            available = max(Decimal("0"), signed_size)
        else:
            available = max(Decimal("0"), -signed_size)

        result["available"] = available
        epsilon = Decimal("0.00000001")

        if available <= epsilon:
            result["skip"] = True
            result["reason"] = "no_position"
            logger.warning(
                f"⚠️ [Lighter] {symbol} 无可平仓持仓，跳过只减仓订单 (side={side})"
            )
            return result

        if quantity > available:
            result["quantity"] = available
            logger.warning(
                f"⚠️ [Lighter] {symbol} 只减仓数量超出持仓，已从 {quantity} 调整为 {available} "
                f"(side={side})"
            )

        return result

    async def _build_signed_market_order_tx(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        *,
        reduce_only: bool = False,
        slippage_multiplier: Decimal = Decimal("1.0"),
        client_order_id: Optional[int] = None,
        target_price: Optional[Decimal] = None
    ) -> Optional[Dict[str, Any]]:
        """构建市价单的签名交易数据，不立即发送"""
        if not self.signer_client:
            logger.error("SignerClient 未初始化，无法签名订单")
            return None

        # 🔥 现货不支持 reduce_only（最后一道防线）
        if reduce_only and "SPOT" in str(symbol).upper():
            logger.warning(
                f"⚠️ [Lighter] 现货市价单不支持 reduce_only，已自动关闭: {symbol}"
            )
            reduce_only = False

        market_info = await self._get_market_info(symbol)
        if not market_info:
            logger.error(f"未找到市场信息: {symbol}")
            return None

        # 🔥 修复：先检查前置条件，避免获取nonce后再失败
        avg_execution_price = await self._calculate_slippage_protection_price(
            symbol,
            side,
            provided_price=target_price,
            slippage_multiplier=slippage_multiplier
        )
        if not avg_execution_price:
            logger.error(f"无法计算{symbol}的滑点保护价格")
            return None

        params = self._convert_market_order_params(
            market_info,
            quantity,
            avg_execution_price,
            side,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )

        # 🔥 修复：只有在所有前置条件都满足后，才获取nonce
        # 这样可以避免nonce被消耗但订单没有发送的情况
        api_key_index, nonce = self.signer_client.get_api_key_nonce(-1, -1)

        import lighter
        sign_result = self.signer_client.sign_create_order(
            market_index=params['market_index'],
            client_order_index=params['client_order_index'],
            base_amount=params['base_amount'],
            price=params['avg_execution_price'],
            order_type=lighter.SignerClient.ORDER_TYPE_MARKET,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            trigger_price=lighter.SignerClient.NIL_TRIGGER_PRICE,
            order_expiry=lighter.SignerClient.DEFAULT_IOC_EXPIRY,
            is_ask=params['is_ask'],
            reduce_only=int(params['reduce_only']),
            nonce=nonce,
        )
        if not isinstance(sign_result, tuple):
            raise RuntimeError("sign_create_order 返回未知类型")

        if len(sign_result) == 4:
            tx_type, tx_info, tx_hash, err = sign_result
        elif len(sign_result) == 3:
            tx_type, tx_info, err = sign_result
            tx_hash = None
        elif len(sign_result) == 2:
            # 兼容旧版 lighter SDK：返回 (tx_info, err)
            tx_info, err = sign_result
            tx_type = getattr(
                lighter.SignerClient,
                "TX_TYPE_CREATE_ORDER",
                14  # 默认值
            )
            tx_hash = None
        else:
            raise RuntimeError("sign_create_order 返回异常结果")

        if err:
            logger.error(f"签名市价单失败: {self.parse_error(err)}")
            # 🔥 修复：签名失败时立即回滚nonce
            try:
                self.signer_client.nonce_manager.acknowledge_failure(
                    api_key_index)
                logger.debug(f"✅ Nonce回滚成功: api_key_index={api_key_index}")
            except Exception as ack_err:
                logger.warning(f"⚠️ Nonce回滚失败: {ack_err}")
            return None

        payload = {
            "tx_type": tx_type,
            "tx_info": tx_info,
            "api_key_index": api_key_index,
            "context": {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "reduce_only": reduce_only,
                "client_order_id": params['client_order_index'],
                "avg_execution_price": avg_execution_price
            }
        }
        if tx_hash is not None:
            payload["tx_hash"] = tx_hash
        return payload

    async def place_market_orders_via_ws_batch(
        self,
        orders: List[Dict[str, Any]],
        *,
        slippage_multiplier: Decimal = Decimal("1.0")
    ) -> Optional[Dict[str, Any]]:
        """
        使用WebSocket批量同时下市价单

        Args:
            orders: 订单列表，每项包含 symbol/side/quantity，可选 reduce_only
            slippage_multiplier: 滑点倍数
        """
        if not self._websocket:
            msg = "WebSocket 模块未初始化，无法发送批量订单"
            logger.error(msg)
            raise RuntimeError(msg)

        signed_payloads: List[Dict[str, Any]] = []
        api_key_indices: List[int] = []
        order_contexts: List[Dict[str, Any]] = []
        skipped_orders: List[Dict[str, Any]] = []

        # 🔥 关键修复：记录签名失败信息，用于判断是否需要回滚
        sign_failed = False
        sign_error_msg = None

        for order in orders:
            symbol = order.get("symbol")
            side = order.get("side")
            qty_raw = Decimal(str(order.get("quantity")))
            reduce_only = bool(order.get("reduce_only", False))
            target_price = order.get("target_price")  # 🔥 获取目标价格（决策引擎提供的价格）
            if target_price is not None:
                target_price = Decimal(str(target_price))

            adjustment = self._prepare_reduce_only_quantity(
                symbol,
                side,
                qty_raw,
                reduce_only
            )
            qty = adjustment["quantity"]
            if adjustment["skip"]:
                skipped_orders.append({
                    "symbol": symbol,
                    "side": side,
                    "reason": adjustment.get("reason", "unknown")
                })
                continue

            try:
                signed = await self._build_signed_market_order_tx(
                    symbol,
                    side,
                    qty,
                    reduce_only=reduce_only,
                    slippage_multiplier=slippage_multiplier,
                    client_order_id=order.get("client_order_id"),
                    target_price=target_price
                )
                if not signed:
                    # 🔥 关键：签名失败，需要回滚所有已获取的nonce
                    sign_failed = True
                    sign_error_msg = f"签名失败: {order}"
                    logger.error(f"❌ {sign_error_msg}")
                    break  # 立即中断，不再处理后续订单

                signed_payloads.append(signed)
                api_key_indices.append(signed["api_key_index"])

            except Exception as sign_err:
                # 🔥 关键：签名异常，需要回滚所有已获取的nonce
                sign_failed = True
                sign_error_msg = f"签名异常: {sign_err}, 订单: {order}"
                logger.error(f"❌ {sign_error_msg}")
                break  # 立即中断，不再处理后续订单
            context = signed.get("context", {})
            context.update({
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "requested_quantity": qty_raw,
                "reduce_only": reduce_only,
                "position_available": adjustment.get("available")
            })
            order_contexts.append(context)

        # 🔥 第二阶段：如果签名失败，回滚所有已获取的nonce
        if sign_failed:
            logger.warning(
                f"⚠️ 批量下单签名阶段失败，回滚已获取的 {len(api_key_indices)} 个nonce")
            for api_key_index in api_key_indices:
                try:
                    self.signer_client.nonce_manager.acknowledge_failure(
                        api_key_index)
                    logger.debug(f"✅ 回滚nonce: api_key_index={api_key_index}")
                except Exception as rollback_err:
                    logger.warning(
                        f"⚠️ 回滚nonce失败: api_key_index={api_key_index}, error={rollback_err}")

            # 抛出异常，终止批量下单
            raise RuntimeError(f"批量下单签名失败，已回滚所有nonce: {sign_error_msg}")

        tx_types = [item["tx_type"] for item in signed_payloads]
        tx_infos = [item["tx_info"] for item in signed_payloads]

        if not signed_payloads:
            logger.info(
                "ℹ️ [Lighter] WS批量市价单无有效订单（全部因无可平仓持仓被跳过）"
            )
            return {
                "orders": [],
                "skipped_orders": skipped_orders
            }

        # 🔥 修复：将整个发送过程包在try块中，确保失败时能回滚nonce
        try:
            response = await self._websocket.send_tx_batch(tx_types, tx_infos)
            # 统一打印一次返回摘要，便于排障：
            # - 若返回中包含 error，说明交易请求并未真正成功（可能被拒绝/限流/参数错误）
            # - 若无 tx_hash，则后续很难从链上/服务端侧追踪
            if isinstance(response, dict) and response.get("error"):
                logger.error("❌ WS批量市价单返回错误: %s", response.get("error"))
                raise RuntimeError(str(response))

            tx_hashes = response.get("tx_hash") if isinstance(response, dict) else None
            if isinstance(tx_hashes, list):
                logger.info(
                    "✅ WS批量市价单已发送，等待成交: tx_hash_count=%s first_tx_hash=%s",
                    len(tx_hashes),
                    tx_hashes[0] if tx_hashes else None,
                )
            else:
                logger.info("✅ WS批量市价单已发送，等待成交")
            if isinstance(tx_hashes, list):
                orders_data: List[OrderData] = []
                for idx, ctx in enumerate(order_contexts):
                    tx_hash = tx_hashes[idx] if idx < len(tx_hashes) else None
                    orders_data.append(
                        self._build_ws_batch_order_placeholder(ctx, tx_hash))
                response["orders"] = orders_data
            else:
                response["orders"] = [
                    self._build_ws_batch_order_placeholder(ctx, None)
                    for ctx in order_contexts
                ]
            if skipped_orders:
                response["skipped_orders"] = skipped_orders
            return response
        except Exception as e:
            logger.error(f"WS批量市价单发送失败: {e}")
            for api_key_index in api_key_indices:
                try:
                    self.signer_client.nonce_manager.acknowledge_failure(
                        api_key_index)
                except Exception:
                    pass
            raise RuntimeError(f"WS批量市价单发送失败: {e}") from e

    def _build_ws_batch_order_placeholder(
        self,
        context: Dict[str, Any],
        tx_hash: Optional[str]
    ) -> OrderData:
        """根据批量下单上下文构建占位OrderData，供上层等待成交"""
        from datetime import datetime

        symbol = context.get("symbol")
        side = (context.get("side") or "").lower()
        quantity = Decimal(str(context.get("quantity", "0")))
        client_order_id = context.get("client_order_id") or int(
            asyncio.get_event_loop().time() * 1000)
        avg_price = context.get("avg_execution_price")

        order_side = OrderSide.BUY if side != "sell" else OrderSide.SELL

        return OrderData(
            id=str(client_order_id),
            client_id=str(client_order_id),
            symbol=symbol,
            side=order_side,
            type=OrderType.MARKET,
            amount=quantity,
            price=avg_price,
            filled=Decimal("0"),
            remaining=quantity,
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.PENDING,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params={
                "reduce_only": context.get("reduce_only", False),
                "ws_batch": True
            },
            raw_data={
                "tx_hash": tx_hash,
                "ws_batch": True
            }
        )

    async def _execute_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        provided_price: Optional[Decimal],
        market_info: Dict,
        **kwargs
    ) -> Optional[OrderData]:
        """执行市价单"""
        # 🔥 生成唯一的 client_order_id（确保整个流程使用同一个值）
        if "client_order_id" not in kwargs:
            kwargs["client_order_id"] = int(
                asyncio.get_event_loop().time() * 1000)

        # 🔥 获取滑点倍数（支持动态调整）
        slippage_multiplier = kwargs.pop("slippage_multiplier", Decimal("1.0"))

        # 计算滑点保护价格
        avg_execution_price = await self._calculate_slippage_protection_price(
            symbol, side, provided_price, slippage_multiplier
        )
        if not avg_execution_price:
            return None

        # 转换参数
        params = self._convert_market_order_params(
            market_info, quantity, avg_execution_price, side, **kwargs
        )

        # 执行下单
        try:
            tx, tx_hash, err = await self.signer_client.create_market_order(**params)

            # 处理结果
            return await self._handle_order_result(
                tx, tx_hash, err, symbol, side, "market",
                quantity, avg_execution_price, **kwargs
            )
        except LighterReduceOnlyError:
            raise
        except Exception as e:
            logger.error(f"执行市价单失败: {e}")
            return None

    async def _execute_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
        market_info: Dict,
        **kwargs
    ) -> Optional[OrderData]:
        """执行限价单"""
        if not price:
            logger.error("限价单必须指定价格")
            return None

        # 🔥 生成唯一的 client_order_id（确保整个流程使用同一个值）
        if "client_order_id" not in kwargs:
            kwargs["client_order_id"] = int(
                asyncio.get_event_loop().time() * 1000)

        # 转换参数
        params = self._convert_limit_order_params(
            market_info, quantity, price, side, **kwargs
        )

        # 执行下单
        try:
            import lighter
            tx, tx_hash, err = await self.signer_client.create_order(**params)

            # 🔥 处理结果（使用调整后的价格，与_convert_limit_order_params保持一致）
            price_decimals = market_info['price_decimals']
            if price_decimals == 0:
                quantize_precision = Decimal("1")
            else:
                quantize_precision = Decimal(10) ** (-price_decimals)

            price_rounded = price.quantize(quantize_precision)

            return await self._handle_order_result(
                tx, tx_hash, err, symbol, side, "limit",
                quantity, price_rounded, **kwargs
            )
        except LighterReduceOnlyError:
            raise
        except Exception as e:
            logger.error(
                f"执行限价单失败: {e}",
                exc_info=True,
                extra={
                    "symbol": symbol,
                    "side": side,
                    "price": str(price),
                    "quantity": str(quantity),
                    "params": params,
                }
            )
            return None

    async def _handle_order_result(
        self,
        tx,
        tx_hash,
        err,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal,
        **kwargs
    ) -> Optional[OrderData]:
        """
        处理下单结果

        ⚠️ 重要：Lighter下单API返回的是transaction hash，不是order_id
        真正的order_id需要从WebSocket推送或REST查询中获取
        """
        logger.debug(
            f"📋 _handle_order_result: side={side}, price={price}, qty={quantity}"
        )

        # 检查错误
        if err:
            error_msg = self.parse_error(err) if err else "未知错误"
            logger.error(f"❌ Lighter下单失败: {error_msg}")
            logger.error(f"   订单类型: {order_type}, 方向: {side}, 数量: {quantity}")
            if order_type == "market":
                logger.error(f"   市价单保护价格: {price}")
            else:
                logger.error(f"   限价单价格: {price}")

            # 🔥 特殊处理：invalid nonce 错误
            # 这通常发生在批量取消订单失败后，nonce 状态被破坏
            if "invalid nonce" in str(error_msg).lower() or "21104" in str(error_msg):
                logger.warning("⚠️ 检测到 invalid nonce 错误")
                logger.warning("💡 原因：可能是批量取消订单失败后，nonce 状态不同步")
                logger.warning("💡 建议：等待 1-2 秒后重试，或检查是否有批量取消操作失败")
                logger.warning("💡 临时解决方案：系统会自动重试，但如果持续失败，可能需要重新初始化连接")

            if self._is_reduce_only_message(error_msg):
                raise LighterReduceOnlyError(error_msg)

            return None

        # 检查返回值
        if not tx and not tx_hash:
            logger.error(f"❌ Lighter下单失败: tx和tx_hash都为空（无错误信息）")
            logger.error(f"   这可能是钱包未授权或gas不足")
            return None

        # 🔥 提取transaction hash（这不是order_id！）
        tx_hash_str = str(tx_hash.tx_hash) if tx_hash and hasattr(
            tx_hash, 'tx_hash') else str(tx_hash)
        logger.debug(f"🌐 [REST] 下单成功: tx_hash={tx_hash_str}")

        # 🔥 Lighter特殊处理：REST API无法立即查询到新下的订单
        # 原因：
        # 1. Lighter是Layer 2，订单需要时间上链
        # 2. account_api.account()不返回订单列表（只返回账户信息）
        # 3. order_api.account_active_orders()需要复杂的认证token
        #
        # 解决方案：
        # 1. 返回带tx_hash的临时OrderData
        # 2. 依赖WebSocket推送真正的order_id和状态
        # 3. 网格系统通过WebSocket回调更新订单信息
        logger.debug(
            f"🌐 [REST] 等待WebSocket推送order_id (tx_hash: {tx_hash_str})")

        # 🔥 新逻辑：不再查询 order_index
        # - 直接使用 client_order_id 作为临时ID
        # - WebSocket 推送会包含真正的 order_index
        # - 反手单完全依赖 WebSocket 推送触发，不需要本地记录 order_index
        from datetime import datetime

        client_order_id_str = str(kwargs.get("client_order_id", int(
            asyncio.get_event_loop().time() * 1000)))
        order_id = client_order_id_str
        logger.debug(
            f"🌐 [REST] 使用临时 client_order_id={order_id}"
        )

        return OrderData(
            # 🔥 id 和 client_id 使用相同的值（order_index 或 client_order_id）
            id=order_id,
            client_id=order_id,  # 统一标识，消除双键问题
            symbol=symbol,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            type=OrderType.MARKET if order_type == "market" else OrderType.LIMIT,
            amount=quantity,
            price=price,
            filled=Decimal("0"),
            remaining=quantity,
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.PENDING,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params=kwargs,
            raw_data={'tx_hash_str': tx_hash_str}  # 🔥 只保存字符串,避免循环引用
        )

    # 🔥 已删除 _query_order_index 方法
    # 新逻辑：完全依赖 WebSocket 推送获取 order_index，不再主动查询

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
        下单（主流程编排）

        Args:
            symbol: 交易对符号
            side: 订单方向 ("buy" 或 "sell")
            order_type: 订单类型 ("limit" 或 "market")
            quantity: 数量
            price: 价格（限价单必需）
            **kwargs: 其他参数

        Returns:
            OrderData对象

        ⚠️  Lighter价格精度说明：
        - Lighter使用动态价格乘数，不同交易对精度不同
        - 价格乘数公式: price_int = price_usd × (10 ** price_decimals)
        - 数量始终使用1e6: base_amount = quantity × 1000000

        示例：
        - ETH (price_decimals=2): $4127.39 × 100 = 412739
        - BTC (price_decimals=1): $114357.8 × 10 = 1143578
        - SOL (price_decimals=3): $199.058 × 1000 = 199058
        - DOGE (price_decimals=6): $0.202095 × 1000000 = 202095

        注意：这与大多数交易所不同！
        - 大多数CEX使用固定的1e8或1e6
        - Lighter根据价格大小动态选择精度，以优化Layer 2性能
        """
        # 🔥 nonce冲突已通过grid_engine_impl.py中的串行下单解决
        # 串行下单确保了nonce自然递增，无需在此添加延迟

        logger.debug(
            f"📝 开始下单: symbol={symbol}, side={side}, type={order_type}, qty={quantity}")

        try:
            # 1. 验证前置条件
            if not self._validate_order_preconditions():
                logger.error(
                    "下单失败：前置条件校验不通过",
                    extra={
                        "symbol": symbol,
                        "side": side,
                        "order_type": order_type,
                        "quantity": str(quantity),
                        "price": str(price),
                    }
                )
                return None

            # 2. 获取市场信息（索引、价格精度、乘数）
            market_info = await self._get_market_info(symbol)
            if not market_info:
                logger.error(
                    "下单失败：未找到市场信息",
                    extra={
                        "symbol": symbol,
                        "side": side,
                        "order_type": order_type,
                        "quantity": str(quantity),
                        "price": str(price),
                    }
                )
                return None

            # 3. 根据订单类型执行下单
            if order_type.lower() == "market":
                return await self._execute_market_order(
                    symbol, side, quantity, price, market_info, **kwargs
                )
            else:
                return await self._execute_limit_order(
                    symbol, side, quantity, price, market_info, **kwargs
                )

        except LighterReduceOnlyError:
            raise
        except Exception as e:
            # 强化错误日志，附加上下文
            logger.error(
                f"下单失败 {symbol}: {e}",
                exc_info=True,
                extra={
                    "symbol": symbol,
                    "side": side,
                    "order_type": order_type,
                    "quantity": str(quantity),
                    "price": str(price),
                }
            )
            return None

    async def set_margin_mode(self, symbol: str, margin_mode: str, leverage: int = 1) -> bool:
        """
        设置交易对的保证金模式

        Args:
            symbol: 交易对符号
            margin_mode: 保证金模式 ("isolated"=逐仓, "cross"=全仓)
            leverage: 杠杆倍数（默认1倍）

        Returns:
            是否设置成功
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法设置保证金模式")
            return False

        try:
            # 获取市场索引
            market_info = await self._get_market_info(symbol)
            if not market_info:
                logger.error(f"未找到市场信息: {symbol}")
                return False

            market_index = market_info['market_index']

            # 转换保证金模式
            import lighter
            if margin_mode.lower() == "isolated":
                margin_mode_value = lighter.SignerClient.ISOLATED_MARGIN_MODE  # 1
            else:
                margin_mode_value = lighter.SignerClient.CROSS_MARGIN_MODE  # 0

            logger.info(f"🔧 设置保证金模式: {symbol} (market_index={market_index}), "
                        f"模式={margin_mode}({margin_mode_value}), 杠杆={leverage}x")

            # 调用 SDK 设置保证金模式和杠杆
            tx, tx_hash, err = await self.signer_client.update_leverage(
                market_index=market_index,
                margin_mode=margin_mode_value,
                leverage=leverage
            )

            if err:
                error_msg = self.parse_error(err)
                logger.error(f"❌ 设置保证金模式失败: {error_msg}")
                return False

            logger.info(
                f"✅ 保证金模式设置成功: {symbol}, {margin_mode}模式, {leverage}x杠杆, tx_hash={tx_hash}")
            return True

        except Exception as e:
            logger.error(f"设置保证金模式异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        取消订单

        Args:
            symbol: 交易对符号
            order_id: 订单ID

        Returns:
            是否成功
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法取消订单")
            return False

        try:
            market_index = self.get_market_index(symbol)
            if market_index is None:
                logger.error(f"未找到交易对 {symbol} 的市场索引")
                return False

            # 🔥 检查order_id是否为tx_hash（128字符十六进制）
            if len(order_id) > 20:  # tx_hash通常是128字符，order_index是整数
                logger.warning(
                    f"⚠️ 订单ID似乎是tx_hash（长度{len(order_id)}），无法直接取消。"
                    f"需要等待WebSocket更新为真实的order_index"
                )
                # 尝试从挂单列表中查找真实的order_index
                try:
                    orders = await self.get_open_orders(symbol)
                    for order in orders:
                        # 通过client_order_id或其他方式匹配
                        # 暂时返回False，等待WebSocket更新
                        pass
                except Exception as e:
                    logger.error(f"查询挂单失败: {e}")
                return False

            # 取消订单
            tx, tx_hash, err = await self.signer_client.cancel_order(
                market_index=market_index,
                order_index=int(order_id),
            )

            if err:
                logger.error(f"取消订单失败: {self.parse_error(err)}")
                return False

            return True

        except Exception as e:
            logger.error(f"取消订单失败 {symbol}/{order_id}: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        批量取消所有订单（Lighter专用批量取消API）

        🔥 使用Lighter的批量取消功能（TX_TYPE_CANCEL_ALL_ORDERS）
        比逐个取消更快速、更可靠

        注意：Lighter的批量取消API会取消所有交易对的所有订单
        如果指定了symbol，批量取消后需要验证该交易对的订单是否已取消

        Args:
            symbol: 交易对符号（可选，批量取消会取消所有交易对的订单）

        Returns:
            被取消的订单列表（取消前的订单列表）
        """
        if not self.signer_client:
            # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
            logger.debug("未配置SignerClient，无法批量取消订单")
            return []

        try:
            import lighter
            import time

            # 🔥 批量取消前，先获取订单列表（用于返回）
            orders_before_cancel = await self.get_open_orders(symbol)
            if not orders_before_cancel:
                logger.info("没有待取消的订单")
                return []

            logger.info(f"📋 准备批量取消 {len(orders_before_cancel)} 个订单...")

            # 🔥 使用批量取消API（IMMEDIATE表示立即取消所有订单）
            # 注意：Lighter的批量取消会取消所有交易对的所有订单
            # 🔥 关键修复：IMMEDIATE 模式时，timestamp_ms 必须为 0（"CancelAllTime should be nil"）
            # 只有 SCHEDULED 模式才需要传真实的时间戳
            try:
                # 使用 IMMEDIATE 模式：立即取消所有订单
                tx, tx_hash, err = await self.signer_client.cancel_all_orders(
                    time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE,
                    timestamp_ms=0  # 🔥 IMMEDIATE 模式必须传 0，不能传当前时间
                )
            except RuntimeError as e:
                # 事件循环关闭错误：检查是否是事件循环问题
                if "Event loop is closed" in str(e):
                    logger.error("⚠️ 事件循环已关闭，无法执行批量取消")
                    logger.warning("💡 建议：跳过批量取消，使用逐个取消或重新初始化连接")
                    # 不降级为逐个取消，因为事件循环已关闭，所有异步操作都会失败
                    return orders_before_cancel
                raise
            except TypeError as e:
                # 参数错误，可能是 SDK 版本不兼容
                logger.error(f"⚠️ cancel_all_orders参数错误（可能SDK版本不兼容）: {e}")
                raise

            if err:
                logger.error(f"批量取消订单失败: {self.parse_error(err)}")
                # 降级：尝试逐个取消
                logger.info("降级为逐个取消模式...")
                return await self._cancel_all_orders_fallback(symbol)

            logger.info(f"✅ 批量取消订单成功，交易哈希: {tx_hash}")

            # 等待交易所处理（链上确认需要时间）
            await asyncio.sleep(2)

            # 验证订单是否真的被取消
            remaining_orders = await self.get_open_orders(symbol)
            if remaining_orders:
                logger.warning(
                    f"⚠️ 批量取消后仍有 {len(remaining_orders)} 个订单未取消，"
                    f"可能是链上确认延迟，等待中..."
                )
                await asyncio.sleep(3)
                remaining_orders = await self.get_open_orders(symbol)

                if remaining_orders:
                    logger.warning(
                        f"⚠️ 仍有 {len(remaining_orders)} 个订单未取消，"
                        f"可能需要手动处理或使用逐个取消"
                    )

            # 返回取消前的订单列表
            return orders_before_cancel

        except Exception as e:
            logger.error(f"批量取消订单失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

            # 🔥 如果事件循环关闭或网络错误，可能破坏了 nonce 状态
            # 重置 signer_client 的连接状态（如果需要）
            if "Event loop is closed" in str(e) or "RuntimeError" in str(type(e).__name__):
                logger.warning("⚠️ 检测到事件循环问题，可能影响后续下单的 nonce 状态")
                logger.warning("💡 建议：等待一段时间后再重试，或重新初始化连接")

            # 降级：尝试逐个取消（但如果事件循环已关闭，这个也会失败）
            logger.info("降级为逐个取消模式...")
            try:
                return await self._cancel_all_orders_fallback(symbol)
            except RuntimeError as e2:
                if "Event loop is closed" in str(e2):
                    logger.error("⚠️ 事件循环已关闭，无法执行逐个取消")
                    return orders_before_cancel
                raise

    async def _cancel_all_orders_fallback(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        降级方案：逐个取消订单（当批量取消失败时使用）

        Args:
            symbol: 交易对符号

        Returns:
            被取消的订单列表
        """
        try:
            orders = await self.get_open_orders(symbol)
            cancelled_orders = []

            for order in orders:
                try:
                    # 🔥 修复：OrderData 使用 id 属性，不是 order_id
                    order_id = order.id
                    cancelled = await self.cancel_order(order.symbol, order_id)
                    if cancelled:
                        cancelled_orders.append(order)
                except Exception as e:
                    # 🔥 修复：使用 order.id 而不是 order.order_id
                    logger.error(f"取消订单 {order.id} 失败: {e}")

            return cancelled_orders

        except Exception as e:
            logger.error(f"逐个取消订单失败: {e}")
            return []

    async def place_market_order(
            self,
            symbol: str,
            side: OrderSide,
            quantity: Decimal,
            reduce_only: bool = False,
            skip_order_index_query: bool = False,
            client_order_id: Optional[str] = None,
            **extra_kwargs) -> Optional[OrderData]:
        """
        下市价单（便捷方法）

        Args:
            symbol: 交易对符号
            side: 订单方向
            quantity: 数量
            reduce_only: 只减仓模式（平仓专用，不会开新仓或加仓）
            skip_order_index_query: 跳过 order_index 查询（Volume Maker 使用）
            client_order_id: 客户端订单ID（可选，用于订单追踪）
            **extra_kwargs: 额外参数（如 slippage_multiplier）

        Returns:
            订单数据 或 None
        """
        logger.debug(
            f"🚀 place_market_order被调用: symbol={symbol}, side={side}, qty={quantity}, "
            f"reduce_only={reduce_only}, client_id={client_order_id or 'auto'}")

        # 转换OrderSide枚举为字符串
        side_str = "buy" if side == OrderSide.BUY else "sell"

        logger.debug(f"   转换side: {side} → {side_str}")

        # 🔥 构建kwargs（向后兼容：client_order_id 为可选参数）
        kwargs = {
            "reduce_only": reduce_only,
            "skip_order_index_query": skip_order_index_query
        }

        # 仅在提供 client_order_id 时传递（不影响网格程序）
        if client_order_id is not None:
            kwargs["client_order_id"] = client_order_id

        # 🔥 合并额外参数（如 slippage_multiplier）
        kwargs.update(extra_kwargs)

        return await self.place_order(
            symbol=symbol,
            side=side_str,  # 🔥 修复：传递字符串而不是枚举
            order_type="market",  # 🔥 修复：传递字符串
            quantity=quantity,
            **kwargs
        )

    # ============= 辅助方法 =============

    def _get_symbol_from_market_index(self, market_index: int) -> str:
        """从市场索引获取符号"""
        market_info = self._markets_cache.get(market_index)
        if market_info:
            return market_info.get("symbol", "")
        return ""

    def _parse_order(self, order_info: Any, symbol: str) -> OrderData:
        """
        解析订单信息

        根据Lighter API文档，Order对象包含:
        - order_index: INTEGER (真正的订单ID)
        - order_id: STRING (order_index的字符串形式)
        - client_order_index: INTEGER
        - client_order_id: STRING
        """
        from datetime import datetime

        # 🔥 获取真正的订单ID (优先使用order_index，然后是order_id)
        order_index = getattr(order_info, 'order_index', None)
        order_id_str = getattr(order_info, 'order_id', None)

        # 如果有order_index，使用它；否则使用order_id
        final_order_id = order_id_str if order_id_str else (
            str(order_index) if order_index is not None else '')

        logger.debug(
            f"解析订单: order_index={order_index}, order_id={order_id_str}, final_id={final_order_id}")

        # 解析数量信息
        initial_amount = self._safe_decimal(
            getattr(order_info, 'initial_base_amount', 0))
        filled_amount = self._safe_decimal(
            getattr(order_info, 'filled_base_amount', 0))
        remaining_amount = self._safe_decimal(
            getattr(order_info, 'remaining_base_amount', 0))

        # 解析价格
        price = self._safe_decimal(getattr(order_info, 'price', 0))
        filled_quote = self._safe_decimal(
            getattr(order_info, 'filled_quote_amount', 0))

        # 计算平均价格
        average_price = filled_quote / filled_amount if filled_amount > 0 else None

        # 🔥 获取 client_order_index (注意是 index 不是 id)
        client_order_index = getattr(order_info, 'client_order_index', None)
        client_id_str = str(
            client_order_index) if client_order_index is not None else ''

        params_extra: Dict[str, Any] = {}
        raw_status = getattr(order_info, 'status', None)
        if raw_status:
            params_extra["lighter_status"] = str(raw_status)

        return OrderData(
            # ✅ 使用真正的order_id（order_index的字符串形式）
            id=final_order_id,
            # 🔥 关键修复：使用 client_order_index 而不是 client_order_id
            client_id=client_id_str,
            symbol=symbol,
            side=self._parse_order_side(getattr(order_info, 'is_ask', False)),
            type=self._parse_order_type(getattr(order_info, 'type', 'limit')),
            amount=initial_amount,
            price=price if price > 0 else None,
            filled=filled_amount,
            remaining=remaining_amount,
            cost=filled_quote,
            average=average_price,
            status=self._parse_order_status(
                getattr(order_info, 'status', 'unknown')),
            timestamp=self._parse_timestamp(
                getattr(order_info, 'timestamp', None)) or datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params=params_extra,
            raw_data=self._safe_serialize_order_info(order_info)
        )

    def _safe_serialize_order_info(self, order_info: Any) -> Dict[str, Any]:
        """
        安全地序列化 order_info 对象,避免循环引用

        Args:
            order_info: Lighter SDK 返回的订单对象

        Returns:
            Dict: 可序列化的字典
        """
        if order_info is None:
            return {}

        try:
            # 如果是字典,直接返回
            if isinstance(order_info, dict):
                return order_info

            # 如果是对象,提取关键属性
            result = {}
            safe_attrs = [
                'order_id', 'order_index', 'client_id', 'market_index',
                'is_ask', 'order_type', 'size', 'price', 'filled_amount',
                'remaining_amount', 'filled_quote', 'status', 'timestamp'
            ]

            for attr in safe_attrs:
                if hasattr(order_info, attr):
                    value = getattr(order_info, attr)
                    # 只保存基本类型
                    if isinstance(value, (str, int, float, bool, type(None))):
                        result[attr] = value
                    else:
                        result[attr] = str(value)

            return result

        except Exception as e:
            logger.warning(f"序列化 order_info 失败: {e}")
            return {"error": "serialization_failed"}

    def _parse_order_side(self, is_ask: bool) -> OrderSide:
        """解析订单方向"""
        return OrderSide.SELL if is_ask else OrderSide.BUY

    def _parse_order_type(self, order_type_str: str) -> OrderType:
        """解析订单类型"""
        type_mapping = {
            'market': OrderType.MARKET,
            'limit': OrderType.LIMIT,
            'stop-limit': OrderType.STOP_LIMIT,
            'stop_limit': OrderType.STOP_LIMIT,
            'stop-market': OrderType.STOP,
            'stop_market': OrderType.STOP,
            'stop': OrderType.STOP,
        }
        return type_mapping.get(order_type_str.lower().replace('_', '-'), OrderType.LIMIT)

    def _parse_order_status(self, status_str: str) -> OrderStatus:
        """解析订单状态"""
        status_mapping = {
            'pending': OrderStatus.PENDING,
            'open': OrderStatus.OPEN,
            'filled': OrderStatus.FILLED,
            'canceled': OrderStatus.CANCELED,
            'canceled-too-much-slippage': OrderStatus.CANCELED,
            'expired': OrderStatus.EXPIRED,
            'rejected': OrderStatus.REJECTED,
        }
        return status_mapping.get(status_str.lower(), OrderStatus.UNKNOWN)
