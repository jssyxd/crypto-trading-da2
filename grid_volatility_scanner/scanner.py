"""
网格波动率扫描器 - Grid Volatility Scanner

主扫描器逻辑：
1. 初始化交易所适配器
2. 加载市场配置
3. 创建虚拟网格
4. 监控价格并更新网格
5. 计算APR并排序
6. 实时更新UI
"""

import asyncio
import logging
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import yaml

from .models.virtual_grid import VirtualGrid
from .models.simulation_result import SimulationResult
from .ui.scanner_ui import ScannerUI
from .core.apr_calculator import APRCalculator
from .core.apr_alert import APRAlertManager


logger = logging.getLogger(__name__)


class GridVolatilityScanner:
    """
    网格波动率扫描器

    功能：
    - 监控所有市场的实时价格
    - 模拟网格交易（不实际下单）
    - 统计循环次数
    - 计算预估APR
    - 生成推荐列表
    """

    def __init__(
        self,
        exchange_adapter,
        config_path: Optional[str] = None
    ):
        """
        初始化扫描器

        Args:
            exchange_adapter: 交易所适配器（如LighterAdapter）
            config_path: 配置文件路径
        """
        self.adapter = exchange_adapter
        self.config_path = config_path or self._get_default_config_path()

        # 配置数据
        self.market_configs: Dict = {}
        self.scanner_config: Dict = {}

        # 虚拟网格字典 {symbol: VirtualGrid}
        self.virtual_grids: Dict[str, VirtualGrid] = {}

        # UI
        self.ui: Optional[ScannerUI] = None

        # 🔔 APR报警管理器
        self.alert_manager: Optional[APRAlertManager] = None

        # 运行状态
        self._running = False
        self._scan_start_time: Optional[datetime] = None

        # 🔥 订阅统计
        self._subscribed_symbols_count = 0  # 已订阅的代币数量
        self._subscribed_symbols_list = []  # 已订阅的代币列表（保留顺序）
        self._failed_subscribe_symbols = []  # 订阅失败的代币列表
        self._received_ticker_symbols = set()  # 实际收到价格推送的代币集合
        self._no_data_symbols = []  # 订阅成功但无数据的代币列表
        
        # 🔥 Ticker接收统计（用于诊断订阅问题）
        self._ticker_received_count = 0  # 收到的ticker总数
        self._ticker_matched_count = 0  # 匹配成功的ticker数
        self._ticker_unmatched_symbols = set()  # 未匹配的symbol集合

        # 🔥 WebSocket连接监控（新增）
        self._last_data_time: Dict[str, datetime] = {}  # 每个symbol的最后数据接收时间
        self._connection_check_interval = 60  # 连接检查间隔（秒）
        self._data_timeout_seconds = 120  # 数据超时阈值（秒）
        self._startup_grace_period = 120  # 启动缓冲期（秒）
        self._is_reconnecting = False  # 是否正在重连
        self._reconnect_count = 0  # 重连次数（重连成功后重置）
        self._total_reconnect_count = 0  # 🔥 总重连次数（从启动后累计，不重置）
        self._reconnect_base_delay = 2.0  # 🔥 基础退避延迟（秒）
        self._reconnect_max_delay = 60.0  # 🔥 最大退避延迟（秒）- 无限重连，延迟上限60秒
        self._last_health_check_log = datetime.now()  # 上次健康检查日志时间
        self._health_check_log_interval = 300  # 健康检查日志间隔（秒）

        logger.info("网格波动率扫描器初始化")

    def _get_default_config_path(self) -> str:
        """获取默认配置文件路径"""
        current_dir = Path(__file__).parent
        return str(current_dir / "config" / "market_config.yaml")

    async def initialize(self):
        """初始化扫描器"""
        logger.info("开始初始化扫描器...")

        # 1. 加载配置文件
        await self._load_config()

        # 2. 连接交易所（如果尚未连接）
        if not hasattr(self.adapter, '_connected') or not self.adapter._connected:
            logger.info("连接交易所...")
            await self.adapter.connect()

        # 3. 获取所有市场
        logger.info("获取市场列表...")
        markets = await self._get_all_markets()
        logger.info(f"获取到 {len(markets)} 个市场")

        # 4. 创建虚拟网格
        logger.info("创建虚拟网格...")
        await self._create_virtual_grids(markets)
        logger.info(f"创建了 {len(self.virtual_grids)} 个虚拟网格")

        # 5. 创建UI
        logger.info("初始化UI...")
        self.ui = ScannerUI()
        self.ui.update_stats(
            total_markets=len(markets),
            active_markets=len(self.virtual_grids)
        )

        # 6. 🔔 初始化APR报警管理器
        apr_threshold = self.scanner_config.get('apr_alert_threshold', 100.0)
        max_alerts = self.scanner_config.get('apr_alert_max_count', 3)
        cooldown = self.scanner_config.get('apr_alert_cooldown_seconds', 300)
        self.alert_manager = APRAlertManager(
            apr_threshold=apr_threshold,
            max_alerts_per_symbol=max_alerts,
            alert_cooldown_seconds=cooldown
        )
        logger.info(f"✅ APR报警管理器已初始化（阈值={apr_threshold}%）")

        logger.info("✅ 扫描器初始化完成")

    async def _load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 提取市场配置
            self.market_configs = {
                k: v for k, v in config.items()
                if k not in ['default', 'scanner_config']
            }

            # 默认配置
            self.default_config = config.get('default', {
                'grid_width_percent': 5.0,
                'grid_interval_percent': 0.5
            })

            # 扫描器配置
            self.scanner_config = config.get('scanner_config', {
                'min_24h_volume_usdc': 100000,
                'scan_duration_seconds': 3600,
                'order_value_usdc': 10,
                'fee_rate_percent': 0.004,
            })

            logger.info(f"配置加载成功: {len(self.market_configs)} 个市场配置")

        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    async def _get_all_markets(self) -> List[Dict]:
        """
        获取所有市场

        Returns:
            市场信息列表
        """
        try:
            # 获取交易所信息（返回ExchangeInfo对象）
            exchange_info = await self.adapter.get_exchange_info()

            # 从ExchangeInfo对象中提取markets字典
            markets_dict = exchange_info.markets if hasattr(
                exchange_info, 'markets') else {}

            logger.info(f"获取到 {len(markets_dict)} 个原始市场")

            # 转换为列表格式
            filtered_markets = []
            for symbol, market_info in markets_dict.items():
                # 跳过稳定币
                if symbol in ['USDT', 'USDC', 'DAI', 'BUSD', 'USDT-USD', 'USDC-USD']:
                    continue

                # 构建标准化的市场信息字典
                market_data = {
                    'symbol': symbol,
                    'info': market_info if isinstance(market_info, dict) else {}
                }

                filtered_markets.append(market_data)

            logger.info(f"过滤后保留 {len(filtered_markets)} 个市场")
            return filtered_markets

        except Exception as e:
            logger.error(f"获取市场列表失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    async def _precreate_virtual_grids(self, symbols: List[str]) -> int:
        """
        批量获取初始价格并预创建虚拟网格

        Args:
            symbols: 代币符号列表

        Returns:
            成功预创建的虚拟网格数量
        """
        precreated_count = 0

        # 批量获取ticker数据（如果交易所支持）
        try:
            # 尝试批量获取所有ticker
            tickers = await self.adapter.get_tickers(symbols)

            # 为每个成功获取价格的代币创建虚拟网格
            for ticker in tickers:
                if not ticker or not ticker.last_price or ticker.last_price <= 0:
                    continue

                symbol = ticker.symbol
                if symbol in self.virtual_grids:
                    # 已经存在，跳过
                    continue

                # 创建虚拟网格
                if await self._create_single_virtual_grid(symbol, Decimal(str(ticker.last_price))):
                    precreated_count += 1

        except Exception as e:
            logger.debug(f"批量获取ticker失败，将使用懒加载模式: {e}")

        # 如果批量获取失败或部分失败，尝试逐个获取（限制并发数）
        if precreated_count < len(symbols):
            logger.info(
                f"📊 批量获取了 {precreated_count}/{len(symbols)} 个价格，尝试逐个获取剩余 {len(symbols) - precreated_count} 个代币...")

            # 分批处理，每批10个，避免过多并发请求
            batch_size = 10
            remaining_symbols = [
                s for s in symbols if s not in self.virtual_grids]

            total_batches = (len(remaining_symbols) +
                             batch_size - 1) // batch_size
            for batch_idx, i in enumerate(range(0, len(remaining_symbols), batch_size), 1):
                batch = remaining_symbols[i:i + batch_size]
                logger.debug(f"⏳ 处理第 {batch_idx}/{total_batches} 批: {batch}")

                # 并发获取这批代币的价格
                tasks = [self._try_create_virtual_grid(
                    symbol) for symbol in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 统计成功数量和失败数量
                batch_success = sum(
                    1 for success in results if success is True)
                batch_failed = len(batch) - batch_success
                precreated_count += batch_success

                logger.debug(
                    f"✅ 第 {batch_idx} 批完成: 成功{batch_success}, 失败{batch_failed}, 累计{precreated_count}/{len(symbols)}")

                # 添加小延迟，避免请求过快
                if i + batch_size < len(remaining_symbols):
                    await asyncio.sleep(0.1)

            logger.info(
                f"📊 逐个获取完成，总共预创建了 {precreated_count}/{len(symbols)} 个虚拟网格")

        return precreated_count

    async def _try_create_virtual_grid(self, symbol: str) -> bool:
        """
        尝试为单个代币创建虚拟网格

        Args:
            symbol: 代币符号

        Returns:
            是否成功创建
        """
        try:
            # 尝试获取ticker
            ticker = await self.adapter.get_ticker(symbol)
            if ticker and ticker.last_price and ticker.last_price > 0:
                return await self._create_single_virtual_grid(symbol, Decimal(str(ticker.last_price)))
        except Exception as e:
            logger.debug(f"获取 {symbol} 价格失败: {e}")

        return False

    async def _create_single_virtual_grid(self, symbol: str, current_price: Decimal) -> bool:
        """
        为单个代币创建虚拟网格

        Args:
            symbol: 代币符号
            current_price: 当前价格

        Returns:
            是否成功创建
        """
        try:
            # 提取基础符号
            base_symbol = symbol.split('-')[0] if '-' in symbol else symbol

            # 获取配置（未配置的使用默认配置）
            if base_symbol in self.market_configs:
                market_config = self.market_configs[base_symbol]
                config_type = "自定义"
            else:
                market_config = self.default_config
                config_type = "默认"

            # 创建虚拟网格
            grid = VirtualGrid(
                symbol=symbol,
                current_price=current_price,
                grid_width_percent=Decimal(
                    str(market_config['grid_width_percent'])),
                grid_interval_percent=Decimal(
                    str(market_config['grid_interval_percent']))
            )

            self.virtual_grids[symbol] = grid

            logger.debug(
                f"✅ 预创建虚拟网格: {symbol:12s} | "
                f"价格=${current_price:>12} | "
                f"配置={config_type:4s}"
            )

            return True

        except Exception as e:
            logger.debug(f"创建虚拟网格失败 {symbol}: {e}")
            return False

    async def _create_virtual_grids(self, markets: List[Dict]):
        """
        为所有市场订阅WebSocket价格流（预创建 + 懒加载模式）

        策略：
        1. 尝试批量获取所有代币的初始价格，如果成功就立即创建虚拟网格
        2. 订阅所有市场的WebSocket，当收到价格推送时动态创建未创建的虚拟网格
        3. 未在配置文件中的市场将使用默认配置

        Args:
            markets: 市场信息列表
        """
        logger.info("📡 使用预创建 + WebSocket订阅模式")
        logger.info(f"🌐 将监控所有市场（未配置的使用默认参数）")
        logger.info(f"🔍 从交易所获取到 {len(markets)} 个市场")

        # 提取需要监控的符号列表
        symbols_to_monitor = []
        skipped_symbols = []

        for market in markets:
            symbol = None
            try:
                # 支持两种格式：字典格式 {'symbol': 'BTC-USD'} 或字符串格式 'BTC-USD'
                if isinstance(market, dict):
                    symbol = market.get('symbol', '')
                elif isinstance(market, str):
                    symbol = market
                else:
                    logger.warning(f"不支持的市场格式: {type(market)}, 值: {market}")
                    continue
                
                if not symbol:
                    continue

                # 提取基础符号（如 BTC-USD → BTC）
                base_symbol = symbol.split('-')[0] if '-' in symbol else symbol
                quote_symbol = symbol.split('-')[1] if '-' in symbol else ''

                # 🔥 跳过稳定币和不适合网格的交易对
                # 稳定币基础符号
                stablecoin_base = ['USDT', 'USDC',
                                   'DAI', 'BUSD', 'TUSD', 'USDD']
                # 外汇交易对（以法币结尾）
                fiat_quotes = ['JPY', 'EUR', 'GBP', 'CAD', 'AUD', 'CHF', 'CNY']

                # 跳过条件：
                # 1. 基础符号是稳定币
                # 2. 计价货币是法币（但保留USD，因为大部分代币都是XXX-USD格式）
                if base_symbol in stablecoin_base:
                    skipped_symbols.append(symbol)
                    continue

                # 如果计价货币是法币（非USD），跳过
                if quote_symbol and quote_symbol in fiat_quotes:
                    skipped_symbols.append(symbol)
                    continue

                symbols_to_monitor.append(symbol)

            except Exception as e:
                # 安全处理：如果symbol未定义，使用market的字符串表示
                error_symbol = symbol if symbol else str(market)
                logger.error(f"处理市场 {error_symbol} 失败: {e}")
                continue

        logger.info(f"📊 将监控 {len(symbols_to_monitor)} 个市场")
        logger.info(f"🚫 跳过 {len(skipped_symbols)} 个稳定币/外汇市场")

        # 🔥 调试：显示前20个监控的市场
        if len(symbols_to_monitor) > 0:
            sample_symbols = symbols_to_monitor[:20]
            logger.info(f"📋 前20个监控市场: {', '.join(sample_symbols)}")

        # 🔥 步骤1: 禁用预创建（避免Lighter API限流429错误）
        # 原因：Lighter API有严格的速率限制，批量请求会触发429
        # 解决方案：完全依赖WebSocket懒加载（WebSocket无限流）
        logger.info(
            f"⚠️  跳过预创建（避免API限流），将通过WebSocket懒加载创建 {len(symbols_to_monitor)} 个虚拟网格")
        precreated_count = 0  # 直接设为0，跳过预创建

        # 🔥 关键：使用Lighter统一回调模式（参考套利监控）
        subscription_count = 0
        failed_count = 0

        # 🔥 Lighter交易所特殊处理：统一回调模式
        # 原因：Lighter的WebSocket连接是共享的，多个订阅使用同一个回调
        # 参考：run_arbitrage_monitor.py 的实现

        # 🔥 构建symbol映射表（优化性能，避免每次都遍历）
        # ticker.symbol (短格式) → monitor_symbol (标准格式)
        symbol_map = {}
        for monitor_symbol in symbols_to_monitor:
            # 提取基础符号
            base = monitor_symbol.split(
                '-')[0] if '-' in monitor_symbol else monitor_symbol
            # 建立映射：基础符号 → 监控符号
            symbol_map[base] = monitor_symbol
            # 同时支持完整符号匹配
            symbol_map[monitor_symbol] = monitor_symbol

        logger.info(f"📋 构建symbol映射表，共 {len(symbol_map)} 个映射")
        
        # 🔥 输出映射表详细信息（前20个，用于调试）
        logger.info("=" * 80)
        logger.info("📋 Symbol映射表详情（前20个）：")
        logger.info("=" * 80)
        for idx, (key, value) in enumerate(list(symbol_map.items())[:20], 1):
            logger.info(f"  {idx}. {key} → {value}")
        if len(symbol_map) > 20:
            logger.info(f"  ... 还有 {len(symbol_map) - 20} 个映射")
        logger.info("=" * 80)

        # 🔥 重置ticker接收统计（用于调试订阅问题）
        # 已在__init__中初始化，这里重置
        self._ticker_received_count = 0
        self._ticker_matched_count = 0
        self._ticker_unmatched_symbols = set()

        # 定义统一回调（只注册一次，处理所有symbol）
        async def unified_ticker_callback(ticker):
            """
            Lighter统一回调：处理所有订阅symbol的ticker更新

            ticker.symbol 是 Lighter 原始格式（如 "BTC", "ETH", "SOL"）
            需要匹配到我们订阅的标准格式（如 "BTC-USD", "ETH-USD"）

            Args:
                ticker: TickerData对象
            """
            try:
                ticker_symbol = getattr(ticker, 'symbol', None)
                if not ticker_symbol:
                    return
                
                self._ticker_received_count += 1

                # 🔥 使用映射表快速查找
                matched_symbol = symbol_map.get(ticker_symbol)

                if matched_symbol:
                    self._ticker_matched_count += 1
                    await self._on_ticker_update(matched_symbol, ticker)
                    
                    # 🔥 每收到100个ticker，输出一次统计（避免日志过多）
                    if self._ticker_matched_count % 100 == 1:
                        logger.debug(
                            f"📊 Ticker统计: 收到{self._ticker_received_count}个, "
                            f"匹配{self._ticker_matched_count}个, "
                            f"未匹配{len(self._ticker_unmatched_symbols)}个"
                        )
                else:
                    # 🔥 记录未匹配的symbol（用于调试）
                    if ticker_symbol not in self._ticker_unmatched_symbols:
                        self._ticker_unmatched_symbols.add(ticker_symbol)
                        logger.warning(
                            f"⚠️  收到未订阅的ticker: {ticker_symbol} "
                            f"(总共{len(self._ticker_unmatched_symbols)}个未匹配)"
                        )

            except Exception as e:
                logger.error(
                    f"统一回调处理失败 (ticker={getattr(ticker, 'symbol', 'unknown')}): {e}",
                    exc_info=True
                )

        # 🔥 优先使用批量订阅（如果适配器支持）
        if hasattr(self.adapter, 'batch_subscribe_tickers'):
            # 使用批量订阅方法（参考套利监控中Lighter的订阅方式）
            logger.info(f"📊 使用批量订阅方法: {len(symbols_to_monitor)} 个代币")
            try:
                await self.adapter.batch_subscribe_tickers(
                    symbols=symbols_to_monitor,
                    callback=unified_ticker_callback
                )
                subscription_count = len(symbols_to_monitor)
                failed_count = 0  # 🔥 批量订阅成功，失败数为0
                self._subscribed_symbols_list = symbols_to_monitor.copy()
                logger.info(f"✅ 批量订阅完成: {subscription_count} 个代币")
            except Exception as e:
                logger.error(f"❌ 批量订阅失败，回退到逐个订阅: {e}")
                import traceback
                logger.debug(f"   详细错误:\n{traceback.format_exc()}")
                # 回退到逐个订阅
                subscription_count = 0
                failed_count = 0
                for idx, symbol in enumerate(symbols_to_monitor):
                    try:
                        if idx == 0:
                            await self.adapter.subscribe_ticker(symbol, unified_ticker_callback)
                        else:
                            await self.adapter.subscribe_ticker(symbol, None)
                        subscription_count += 1
                        self._subscribed_symbols_list.append(symbol)
                    except Exception as e2:
                        failed_count += 1
                        self._failed_subscribe_symbols.append((symbol, str(e2)))
                        logger.error(f"❌ 订阅失败: {symbol} | 原因: {e2}")
        else:
            # 🔥 适配器不支持批量订阅，使用分批逐个订阅（优化版）
            batch_size = 20  # 每批20个
            total_batches = (len(symbols_to_monitor) + batch_size - 1) // batch_size
            
            logger.info(f"📊 使用分批订阅策略: 每批{batch_size}个，共{total_batches}批")
            
            for batch_idx in range(total_batches):
                start_idx = batch_idx * batch_size
                end_idx = min((batch_idx + 1) * batch_size, len(symbols_to_monitor))
                batch_symbols = symbols_to_monitor[start_idx:end_idx]
                
                logger.info(f"📡 正在订阅第{batch_idx + 1}/{total_batches}批: {len(batch_symbols)}个代币...")
                
                # 订阅本批次的所有symbol
                for idx, symbol in enumerate(batch_symbols):
                    try:
                        absolute_idx = start_idx + idx
                        
                        if absolute_idx == 0:
                            # 🔥 第一个symbol：注册统一回调
                            await self.adapter.subscribe_ticker(
                                symbol=symbol,
                                callback=unified_ticker_callback
                            )
                            logger.info(f"✅ {symbol} (首次注册统一回调)")
                        else:
                            # 🔥 后续symbol：传None复用统一回调
                            await self.adapter.subscribe_ticker(
                                symbol=symbol,
                                callback=None
                            )

                        subscription_count += 1
                        # 🔥 记录成功订阅的代币
                        self._subscribed_symbols_list.append(symbol)
                        logger.debug(f"📡 订阅成功: {symbol} (#{subscription_count})")

                        # 每5个订阅添加小延迟，避免消息发送过快
                        if subscription_count % 5 == 0:
                            await asyncio.sleep(0.05)  # 50ms延迟

                    except Exception as e:
                        failed_count += 1
                        # 🔥 记录订阅失败的代币和原因
                        self._failed_subscribe_symbols.append((symbol, str(e)))
                        logger.error(f"❌ 订阅失败: {symbol} | 原因: {e}")
                        import traceback
                        logger.debug(f"   详细错误:\n{traceback.format_exc()}")
                        continue

                # 🔥 每批之间等待更长时间，确保WebSocket消息发送完毕
                if batch_idx < total_batches - 1:
                    logger.info(f"⏸️  第{batch_idx + 1}批完成，等待1秒后继续...")
                    await asyncio.sleep(1.0)
                
                logger.info(f"✅ 第{batch_idx + 1}/{total_batches}批订阅完成: 已订阅{subscription_count}个，失败{failed_count}个")

        # 🔥 记录订阅数量
        self._subscribed_symbols_count = subscription_count
        
        logger.info("=" * 80)
        logger.info("📊 订阅完成统计")
        logger.info("=" * 80)
        logger.info(f"✅ 订阅成功: {subscription_count} 个")
        logger.info(f"❌ 订阅失败: {failed_count} 个")
        logger.info(f"📡 总计尝试: {subscription_count + failed_count} 个")
        
        # 🔥 输出订阅成功的代币列表（前30个，用于调试）
        if self._subscribed_symbols_list:
            logger.info("=" * 80)
            logger.info(f"📋 成功订阅的代币列表（前30个）：")
            logger.info("=" * 80)
            for idx, symbol in enumerate(self._subscribed_symbols_list[:30], 1):
                logger.info(f"  {idx}. {symbol}")
            if len(self._subscribed_symbols_list) > 30:
                logger.info(f"  ... 还有 {len(self._subscribed_symbols_list) - 30} 个")
            logger.info("=" * 80)
        
        # 如果有订阅失败的代币，立即输出列表
        if self._failed_subscribe_symbols:
            logger.warning("=" * 80)
            logger.warning(f"⚠️  订阅失败的代币列表 ({len(self._failed_subscribe_symbols)}个):")
            logger.warning("=" * 80)
            for idx, (symbol, error) in enumerate(self._failed_subscribe_symbols, 1):
                logger.warning(f"  {idx}. {symbol} | 原因: {error}")
            logger.warning("=" * 80)
        
        logger.info(f"⏳ 等待价格推送并动态创建虚拟网格...")
        logger.info(f"💡 提示: Lighter交易所只推送有交易活动的市场，无交易的市场不会显示")
        logger.info(f"   如果显示的代币数<{subscription_count}，说明部分市场暂时无交易活动")
        logger.info(f"📊 将在运行5分钟后生成详细的订阅统计报告")
        logger.info("=" * 80)
        
        # 🔥 更新UI的订阅统计信息（让用户实时看到订阅状态）
        if self.ui:
            self.ui.update_subscription_stats(
                subscribed=subscription_count,
                failed=failed_count,
                received=0  # 初始时还没有收到数据
            )

    async def _on_ticker_update(self, symbol: str, ticker):
        """
        处理WebSocket ticker更新（懒加载虚拟网格）

        Args:
            symbol: 交易对符号
            ticker: TickerData对象，包含价格等信息
        """
        try:
            # 提取价格
            if not ticker or not ticker.last or ticker.last <= 0:
                return

            current_price = Decimal(str(ticker.last))
            
            # 🔥 更新最后数据接收时间（用于连接监控）
            self._last_data_time[symbol] = datetime.now()
            
            # 🔥 记录收到价格推送的代币（用于统计）
            if symbol not in self._received_ticker_symbols:
                self._received_ticker_symbols.add(symbol)
                # 首次收到时记录日志
                logger.debug(f"📡 首次收到价格推送: {symbol} = ${current_price}")

            # 如果虚拟网格尚未创建，现在创建它（使用统一的方法）
            if symbol not in self.virtual_grids:
                # 🔥 使用统一的方法创建虚拟网格
                if await self._create_single_virtual_grid(symbol, current_price):
                    # 日志：显示配置类型和参数
                    grid = self.virtual_grids[symbol]
                    base_symbol = symbol.split(
                        '-')[0] if '-' in symbol else symbol
                    if base_symbol in self.market_configs:
                        config_type = "自定义"
                        market_config = self.market_configs[base_symbol]
                    else:
                        config_type = "默认"
                        market_config = self.default_config

                    logger.info(
                        f"🎯 WebSocket创建虚拟网格: {symbol:12s} | "
                        f"价格=${current_price:>12} | "
                        f"配置={config_type:4s} | "
                        f"宽度={market_config['grid_width_percent']:>4}% | "
                        f"间距={market_config['grid_interval_percent']:>4}% | "
                        f"网格数={grid.grid_count:>2}"
                    )

                    # 更新UI统计（total_markets = 已创建的虚拟网格数）
                    if self.ui:
                        self.ui.update_stats(
                            total_markets=len(self.virtual_grids),
                            active_markets=len(self.virtual_grids)
                        )

            # 调用原有的价格更新处理
            await self._price_update_callback(symbol, current_price)

        except Exception as e:
            logger.error(f"处理ticker更新失败 {symbol}: {e}")
            import traceback
            logger.debug(traceback.format_exc())

    async def _price_update_callback(self, symbol: str, price: Decimal):
        """
        价格更新回调

        Args:
            symbol: 交易对符号
            price: 新价格
        """
        if symbol not in self.virtual_grids:
            return

        grid = self.virtual_grids[symbol]

        # 更新价格并检测穿越
        cross_direction = grid.update_price(price)

        if cross_direction:
            # 计算APR（使用5分钟滚动窗口）
            grid.calculate_apr(
                order_value_usdc=Decimal(
                    str(self.scanner_config['order_value_usdc'])),
                fee_rate_percent=Decimal(
                    str(self.scanner_config['fee_rate_percent'])),
                time_window_minutes=self.scanner_config.get(
                    'apr_time_window_minutes', 5)
            )

            # 🔔 检查APR是否超过阈值并触发报警
            if self.alert_manager and grid.estimated_apr > 0:
                self.alert_manager.check_and_alert(symbol, grid.estimated_apr)

            # 记录穿越日志
            logger.debug(
                f"{symbol} {cross_direction} 穿越: "
                f"价格=${price:.2f}, "
                f"循环={grid.complete_cycles}, "
                f"APR={grid.estimated_apr:.2f}%"
            )

    async def _monitor_prices(self):
        """
        监控价格更新

        使用轮询方式获取价格（简化实现）
        实际应该集成WebSocket实时推送
        """
        logger.info("开始价格监控（轮询模式）...")

        update_interval = 1  # 每秒更新一次

        while self._running:
            try:
                # 批量获取所有市场的价格
                for symbol in list(self.virtual_grids.keys()):
                    try:
                        ticker = await self.adapter.get_ticker(symbol)
                        if ticker and ticker.last_price:
                            price = Decimal(str(ticker.last_price))
                            await self._price_update_callback(symbol, price)
                    except Exception as e:
                        logger.warning(f"更新 {symbol} 价格失败: {e}")

                await asyncio.sleep(update_interval)

            except Exception as e:
                logger.error(f"价格监控循环错误: {e}")
                await asyncio.sleep(5)

    async def _generate_subscription_report(self):
        """
        生成详细的订阅统计报告
        
        包括：
        1. 订阅失败的代币
        2. 订阅成功但无数据的代币
        3. 成功接收数据的代币
        """
        received_count = len(self._received_ticker_symbols)
        subscribed_count = self._subscribed_symbols_count
        failed_count = len(self._failed_subscribe_symbols)
        
        # 计算订阅成功但未收到数据的代币
        subscribed_set = set(self._subscribed_symbols_list)
        no_data_symbols = subscribed_set - self._received_ticker_symbols
        self._no_data_symbols = sorted(no_data_symbols)
        
        # 生成控制台报告
        logger.info("=" * 80)
        logger.info("📊 订阅统计报告（运行5分钟）")
        logger.info("=" * 80)
        logger.info(f"📡 尝试订阅的代币总数: {subscribed_count + failed_count}")
        logger.info(f"✅ 订阅成功: {subscribed_count}")
        logger.info(f"❌ 订阅失败: {failed_count}")
        logger.info(f"📈 收到价格推送: {received_count} ({received_count/subscribed_count*100 if subscribed_count > 0 else 0:.1f}%)")
        logger.info(f"🚫 订阅成功但无数据: {len(no_data_symbols)} ({len(no_data_symbols)/subscribed_count*100 if subscribed_count > 0 else 0:.1f}%)")
        
        # 🔥 Ticker接收统计（用于判断WebSocket回调是否正常工作）
        logger.info("=" * 80)
        logger.info("📡 WebSocket Ticker接收统计：")
        logger.info(f"  总接收数: {self._ticker_received_count} 个ticker推送")
        logger.info(f"  匹配成功: {self._ticker_matched_count} 个")
        logger.info(f"  未匹配: {len(self._ticker_unmatched_symbols)} 个不同的symbol")
        if self._ticker_unmatched_symbols:
            unmatched_list = sorted(list(self._ticker_unmatched_symbols))[:10]
            logger.info(f"  未匹配示例（前10个）: {', '.join(unmatched_list)}")
        
        # 🔥 关键诊断信息
        if self._ticker_received_count == 0:
            logger.error("❌ 警告: 没有收到任何ticker推送！")
            logger.error("   可能原因：")
            logger.error("   1. WebSocket连接未建立")
            logger.error("   2. 回调函数未被触发")
            logger.error("   3. Lighter交易所数据推送异常")
        elif self._ticker_matched_count == 0:
            logger.error("❌ 警告: 收到ticker推送但全部未匹配！")
            logger.error("   可能原因：")
            logger.error("   1. Symbol映射表配置错误")
            logger.error("   2. 订阅的symbol格式与ticker返回格式不一致")
        elif self._ticker_matched_count < received_count:
            logger.warning(f"⚠️  注意: 匹配成功的ticker({self._ticker_matched_count})少于实际收到数据的代币({received_count})")
        
        logger.info("=" * 80)
        
        # 写入详细报告到日志文件
        from pathlib import Path
        log_dir = Path(__file__).parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_file = log_dir / f"subscription_report_{timestamp}.log"
        
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("📊 网格波动率扫描器 - 订阅统计详细报告\n")
            f.write("=" * 80 + "\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"运行时长: 5分钟\n\n")
            
            # 1. 总览
            f.write("【总览】\n")
            f.write(f"  尝试订阅: {subscribed_count + failed_count} 个代币\n")
            f.write(f"  订阅成功: {subscribed_count} 个\n")
            f.write(f"  订阅失败: {failed_count} 个\n")
            f.write(f"  收到数据: {received_count} 个 ({received_count/subscribed_count*100 if subscribed_count > 0 else 0:.1f}%)\n")
            f.write(f"  无数据推送: {len(no_data_symbols)} 个 ({len(no_data_symbols)/subscribed_count*100 if subscribed_count > 0 else 0:.1f}%)\n\n")
            
            # 2. 订阅失败的代币
            if self._failed_subscribe_symbols:
                f.write("=" * 80 + "\n")
                f.write(f"【订阅失败】共 {len(self._failed_subscribe_symbols)} 个代币\n")
                f.write("=" * 80 + "\n")
                for idx, (symbol, error) in enumerate(self._failed_subscribe_symbols, 1):
                    f.write(f"{idx}. {symbol}\n")
                    f.write(f"   原因: {error}\n\n")
            else:
                f.write("【订阅失败】无\n\n")
            
            # 3. 订阅成功但无数据的代币
            if no_data_symbols:
                f.write("=" * 80 + "\n")
                f.write(f"【订阅成功但无数据】共 {len(no_data_symbols)} 个代币\n")
                f.write("=" * 80 + "\n")
                f.write("说明: 这些代币订阅成功，但5分钟内未收到价格推送\n")
                f.write("可能原因:\n")
                f.write("  1. 交易活动极低，暂时无价格更新\n")
                f.write("  2. 市场已下架或停止交易\n")
                f.write("  3. WebSocket订阅消息未生效（需要重启扫描器）\n\n")
                
                # 按字母顺序排序并分组显示
                sorted_symbols = sorted(no_data_symbols)
                for idx, symbol in enumerate(sorted_symbols, 1):
                    f.write(f"{idx}. {symbol}\n")
                f.write("\n")
            else:
                f.write("【订阅成功但无数据】无\n\n")
            
            # 4. 成功接收数据的代币
            f.write("=" * 80 + "\n")
            f.write(f"【成功接收数据】共 {received_count} 个代币\n")
            f.write("=" * 80 + "\n")
            sorted_received = sorted(self._received_ticker_symbols)
            for idx, symbol in enumerate(sorted_received, 1):
                # 获取该代币的虚拟网格信息
                if symbol in self.virtual_grids:
                    grid = self.virtual_grids[symbol]
                    f.write(f"{idx}. {symbol:15s} | 循环: {grid.complete_cycles:3d} | APR: {grid.estimated_apr:7.2f}%\n")
                else:
                    f.write(f"{idx}. {symbol}\n")
            f.write("\n")
            
            f.write("=" * 80 + "\n")
            f.write("报告结束\n")
            f.write("=" * 80 + "\n")
        
        logger.info(f"📄 详细报告已保存: {report_file}")
        logger.info(f"💡 提示: 如果无数据代币过多（>50%），建议重启扫描器")
        logger.info("=" * 80)

    async def _update_ui_loop(self):
        """UI更新循环"""
        logger.info("开始UI更新循环...")
        
        # 🔥 订阅统计标志（只显示一次）
        subscription_stats_logged = False

        while self._running:
            try:
                # 🔥 定期重新计算所有网格的APR（即使没有新穿越）
                # 这样可以：
                # 1. 清理过期的循环事件（超过5分钟窗口）
                # 2. 更新cycles_per_hour为最新的5分钟数据
                # 3. 即使代币暂时不波动，也能反映实时状态
                for symbol, grid in self.virtual_grids.items():
                    grid.calculate_apr(
                        order_value_usdc=Decimal(
                            str(self.scanner_config['order_value_usdc'])),
                        fee_rate_percent=Decimal(
                            str(self.scanner_config['fee_rate_percent'])),
                        time_window_minutes=self.scanner_config.get(
                            'apr_time_window_minutes', 5)
                    )

                    # 🔔 检查APR是否超过阈值并触发报警
                    if self.alert_manager and grid.estimated_apr > 0:
                        self.alert_manager.check_and_alert(
                            symbol, grid.estimated_apr)

                # 收集所有结果
                results = []
                min_cycles = self.scanner_config.get(
                    'min_cycles_to_display', 0)
                
                # 1️⃣ 添加有交易活动的代币（已创建虚拟网格的）
                for grid in self.virtual_grids.values():
                    # 🔥 根据配置决定是否显示：
                    # - min_cycles_to_display=0: 显示所有虚拟网格（包括循环为0的）
                    # - min_cycles_to_display>0: 只显示循环次数>=min_cycles的，但BTC例外（即使循环为0也显示）
                    symbol_upper = grid.symbol.upper()
                    is_btc = 'BTC' in symbol_upper and not any(
                        x in symbol_upper for x in ['WBTC', 'TBTC', 'RBTC'])

                    if min_cycles == 0 or grid.complete_cycles >= min_cycles or is_btc:
                        result = SimulationResult.from_virtual_grid(grid)
                        results.append(result)
                
                # 2️⃣ 添加订阅成功但无交易活动的代币（占位符）
                subscribed_symbols = set(self._subscribed_symbols_list)
                received_symbols = set(self._received_ticker_symbols)
                no_activity_symbols = subscribed_symbols - received_symbols
                
                for symbol in sorted(no_activity_symbols):
                    # 🔥 创建"无交易活动"的占位符结果
                    placeholder = SimulationResult.create_no_activity_placeholder(symbol)
                    results.append(placeholder)

                # 更新UI
                if self.ui:
                    self.ui.update_results(results)
                    self.ui.update_stats(
                        total_markets=len(self.virtual_grids),
                        active_markets=len(
                            [g for g in self.virtual_grids.values() if g.complete_cycles > 0])
                    )
                    # 🔥 更新订阅统计（实时显示收到数据的代币数量）
                    self.ui.update_subscription_stats(
                        subscribed=self._subscribed_symbols_count,
                        failed=len(self._failed_subscribe_symbols),
                        received=len(self._received_ticker_symbols)
                    )
                
                # 🔥 在运行5分钟后显示订阅统计（只显示一次）
                if not subscription_stats_logged and self._scan_start_time:
                    elapsed_seconds = (datetime.now() - self._scan_start_time).total_seconds()
                    if elapsed_seconds >= 300:  # 5分钟
                        # 生成详细统计报告
                        await self._generate_subscription_report()
                        subscription_stats_logged = True

                await asyncio.sleep(0.5)  # 每0.5秒更新一次UI

            except Exception as e:
                logger.error(f"UI更新循环错误: {e}")
                await asyncio.sleep(1)

    async def scan(self, duration_seconds: Optional[int] = None):
        """
        开始扫描

        Args:
            duration_seconds: 扫描时长（秒），None表示持续运行直到用户中断
        """
        self._running = True
        self._scan_start_time = datetime.now()

        if duration_seconds is None:
            logger.info("🎯 开始持续监控模式（按 Ctrl+C 停止）")
            logger.info("📊 APR计算: 实时滚动计算过去5分钟的数据")
        else:
            logger.info(f"🎯 开始定时扫描模式，运行 {duration_seconds} 秒")

        try:
            # 启动UI更新任务
            ui_update_task = asyncio.create_task(self._update_ui_loop())
            
            # 🔥 启动WebSocket连接监控任务
            connection_monitor_task = asyncio.create_task(self._monitor_websocket_connection())

            # 运行UI（阻塞直到扫描结束或用户中断）
            if self.ui:
                if duration_seconds is None:
                    # 持续运行模式：传递None，UI会持续显示直到Ctrl+C
                    await self.ui.run(scan_duration=None)
                else:
                    # 定时模式：传递具体时长
                    await self.ui.run(scan_duration=duration_seconds)
            else:
                if duration_seconds is None:
                    # 没有UI时，持续运行直到被中断
                    while self._running:
                        await asyncio.sleep(1)
                else:
                    await asyncio.sleep(duration_seconds)

            # 停止任务
            self._running = False
            ui_update_task.cancel()
            connection_monitor_task.cancel()  # 🔥 取消连接监控任务

            # 等待任务完成
            try:
                await ui_update_task
            except asyncio.CancelledError:
                pass
            
            try:
                await connection_monitor_task
            except asyncio.CancelledError:
                pass

            logger.info("扫描完成")

        except KeyboardInterrupt:
            logger.info("用户中断扫描")
            self._running = False
        except Exception as e:
            logger.error(f"扫描过程错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            await self.cleanup()

    async def cleanup(self):
        """清理资源"""
        logger.info("清理资源...")

        try:
            # 断开交易所连接
            if self.adapter:
                await self.adapter.disconnect()

            # 停止UI
            if self.ui:
                self.ui.stop()

            logger.info("资源清理完成")
        except Exception as e:
            logger.error(f"清理资源失败: {e}")

    def get_results(self) -> List[SimulationResult]:
        """
        获取扫描结果

        Returns:
            按APR排序的模拟结果列表（BTC永远排第一）
        """
        results = []
        min_cycles = self.scanner_config.get('min_cycles_to_display', 0)
        for grid in self.virtual_grids.values():
            # 🔥 根据配置决定是否显示：
            # - min_cycles_to_display=0: 显示所有虚拟网格（包括循环为0的）
            # - min_cycles_to_display>0: 只显示循环次数>=min_cycles的，但BTC例外（即使循环为0也显示）
            symbol_upper = grid.symbol.upper()
            is_btc = 'BTC' in symbol_upper and not any(
                x in symbol_upper for x in ['WBTC', 'TBTC', 'RBTC'])

            if min_cycles == 0 or grid.complete_cycles >= min_cycles or is_btc:
                result = SimulationResult.from_virtual_grid(grid)
                results.append(result)

        # 🔥 自定义排序：BTC永远第一，其他按APR排序
        def sort_key(result):
            # 检查是否为BTC（匹配 BTC, BTC-USD, BTCUSDT 等）
            symbol_upper = result.symbol.upper()
            is_btc = 'BTC' in symbol_upper and not any(
                x in symbol_upper for x in ['WBTC', 'TBTC', 'RBTC'])

            if is_btc:
                # BTC返回极高值，确保排第一
                return (float('inf'), float(result.estimated_apr))
            else:
                # 其他代币按APR排序
                return (0, float(result.estimated_apr))

        results.sort(key=sort_key, reverse=True)

        return results

    def print_summary(self):
        """打印扫描摘要"""
        results = self.get_results()

        if not results:
            print("\n⚠️ 没有有效结果")
            return

        print("\n" + "="*80)
        print("📊 扫描结果摘要")
        print("="*80)
        print(f"监控市场数: {len(self.virtual_grids)}")
        print(f"有效结果数: {len(results)}")

        # 显示Top 10
        print("\n🏆 Top 10 推荐:")
        print("-"*80)
        for i, result in enumerate(results[:10], 1):
            print(
                f"{i:2d}. {result.symbol:<12} "
                f"APR: {result.estimated_apr:>8.2f}%  "
                f"循环: {result.complete_cycles:>4}次  "
                f"评级: {result.rating}"
            )

        print("="*80)

    # === 🔥 WebSocket连接监控相关方法（新增）===
    
    async def _monitor_websocket_connection(self):
        """
        监控WebSocket连接健康状态
        
        定期检查数据更新情况：
        - 启动后120秒内不检查（给足够时间接收首次数据）
        - 如果数据超过阈值时间未更新，触发重连
        - 防止并发重连
        """
        logger.info(
            f"🔄 WebSocket连接监控循环已启动 "
            f"(检查间隔: {self._connection_check_interval}秒, "
            f"启动缓冲期: {self._startup_grace_period}秒, "
            f"超时阈值: {self._data_timeout_seconds}秒)"
        )
        
        while self._running:
            try:
                current_time = datetime.now()
                
                # 启动缓冲期检查
                elapsed_since_start = (current_time - self._scan_start_time).total_seconds()
                if elapsed_since_start < self._startup_grace_period:
                    remaining = self._startup_grace_period - elapsed_since_start
                    # 只在缓冲期前半段输出
                    if remaining > 60 and remaining % 60 < self._connection_check_interval:
                        logger.info(
                            f"⏳ 连接监控启动缓冲期中，剩余 {remaining:.0f} 秒后开始检查"
                        )
                    await asyncio.sleep(self._connection_check_interval)
                    continue
                
                # 🔥 改进：区分活跃代币和不活跃代币，避免误判
                
                # 1. 识别活跃代币（最近有交易活动的）
                active_symbols = []
                inactive_symbols = []
                
                for symbol, grid in self.virtual_grids.items():
                    # 如果有任何交易穿越，说明是活跃代币
                    if grid.total_crosses > 0:
                        active_symbols.append(symbol)
                    else:
                        inactive_symbols.append(symbol)
                
                # 2. 分别检查活跃代币和全部代币的超时情况
                stale_active = []
                stale_inactive = []
                
                for symbol in active_symbols:
                    if self._is_data_stale(symbol, current_time):
                        stale_active.append(symbol)
                
                for symbol in inactive_symbols:
                    if self._is_data_stale(symbol, current_time):
                        stale_inactive.append(symbol)
                
                # 3. 计算比例
                total_symbols = len(self.virtual_grids)
                active_count = len(active_symbols)
                inactive_count = len(inactive_symbols)
                
                active_stale_ratio = len(stale_active) / active_count if active_count > 0 else 0
                total_stale_ratio = (len(stale_active) + len(stale_inactive)) / total_symbols if total_symbols > 0 else 0
                
                # 4. 🔥 智能判断：优先关注活跃代币的超时情况
                should_reconnect = False
                reconnect_reason = ""
                
                if active_count >= 10:
                    # 如果有足够多的活跃代币（>=10个）
                    # 判断依据：如果超过60%的活跃代币超时，说明大概率是连接问题
                    if active_stale_ratio > 0.6:
                        should_reconnect = True
                        reconnect_reason = f"{len(stale_active)}/{active_count}个活跃代币超时 ({active_stale_ratio*100:.0f}%)"
                elif active_count > 0:
                    # 如果活跃代币较少（<10个），要求稍高的超时比例
                    if active_stale_ratio > 0.7:
                        should_reconnect = True
                        reconnect_reason = f"{len(stale_active)}/{active_count}个活跃代币超时 ({active_stale_ratio*100:.0f}%)"
                else:
                    # 如果没有活跃代币，检查全部代币
                    # 要求80%以上超时才重连（避免误判无交易活动）
                    if total_stale_ratio > 0.8:
                        should_reconnect = True
                        reconnect_reason = f"{len(stale_active) + len(stale_inactive)}/{total_symbols}个代币超时 ({total_stale_ratio*100:.0f}%，无活跃代币)"
                
                # 5. 触发重连
                if should_reconnect and not self._is_reconnecting:
                    logger.warning(
                        f"⚠️  检测到连接异常: {reconnect_reason}"
                    )
                    logger.info(
                        f"   活跃代币: {active_count}个 (超时{len(stale_active)}个) | "
                        f"不活跃代币: {inactive_count}个 (超时{len(stale_inactive)}个)"
                    )
                    
                    # 🔥 无限重连：直接触发重连，不检查次数限制
                    asyncio.create_task(self._reconnect_websocket())
                
                # 定期输出健康检查日志（每5分钟一次）
                time_since_last_log = (current_time - self._last_health_check_log).total_seconds()
                if time_since_last_log >= self._health_check_log_interval:
                    self._log_connection_health(current_time)
                    self._last_health_check_log = current_time
                
                # 等待下次检查
                await asyncio.sleep(self._connection_check_interval)
                
            except asyncio.CancelledError:
                logger.info("连接监控循环已取消")
                break
            except Exception as e:
                logger.error(f"❌ 连接监控循环异常: {e}", exc_info=True)
                await asyncio.sleep(10)  # 出错后等待10秒再继续
    
    def _is_data_stale(self, symbol: str, current_time: datetime) -> bool:
        """
        检查指定符号的数据是否过期
        
        Args:
            symbol: 交易对符号
            current_time: 当前时间
            
        Returns:
            bool: 数据是否过期
        """
        last_update = self._last_data_time.get(symbol)
        
        # 如果从未收到数据，认为是过期的
        if not last_update:
            return True
        
        # 计算距离上次更新的时间
        elapsed = (current_time - last_update).total_seconds()
        
        # 超过阈值认为过期
        return elapsed > self._data_timeout_seconds
    
    def _log_connection_health(self, current_time: datetime):
        """
        输出连接健康状态日志
        
        定期输出数据更新情况，帮助用户了解系统状态
        
        Args:
            current_time: 当前时间
        """
        logger.info("=" * 60)
        logger.info("📊 WebSocket 连接健康检查")
        
        # 🔥 区分活跃代币和不活跃代币
        active_symbols = []
        inactive_symbols = []
        
        for symbol, grid in self.virtual_grids.items():
            if grid.total_crosses > 0:
                active_symbols.append(symbol)
            else:
                inactive_symbols.append(symbol)
        
        # 统计活跃代币的数据状态
        active_stale = 0
        active_min_elapsed = None
        active_max_elapsed = None
        
        for symbol in active_symbols:
            last_update = self._last_data_time.get(symbol)
            
            if not last_update:
                active_stale += 1
                continue
            
            elapsed = (current_time - last_update).total_seconds()
            
            if self._is_data_stale(symbol, current_time):
                active_stale += 1
            
            # 更新最小/最大时间差
            if active_min_elapsed is None or elapsed < active_min_elapsed:
                active_min_elapsed = elapsed
            if active_max_elapsed is None or elapsed > active_max_elapsed:
                active_max_elapsed = elapsed
        
        # 统计不活跃代币的数据状态
        inactive_stale = 0
        for symbol in inactive_symbols:
            if self._is_data_stale(symbol, current_time):
                inactive_stale += 1
        
        # 输出总体状态
        active_healthy = len(active_symbols) - active_stale
        inactive_healthy = len(inactive_symbols) - inactive_stale
        
        # 判断总体状态（与重连阈值保持一致）
        if len(active_symbols) > 0:
            active_health_ratio = active_healthy / len(active_symbols)
            if active_health_ratio >= 0.85:
                status = "✅ 正常"
            elif active_health_ratio >= 0.6:  # 60%以下会触发重连
                status = "⚠️  轻微异常"
            else:
                status = "❌ 严重异常（即将重连）"
        else:
            status = "🔶 无活跃代币"
        
        logger.info(f"  交易所: Lighter | 状态: {status}")
        logger.info(
            f"  活跃代币: {active_healthy}/{len(active_symbols)} 健康 | "
            f"不活跃代币: {inactive_healthy}/{len(inactive_symbols)} 健康"
        )
        
        if active_min_elapsed is not None and active_max_elapsed is not None:
            logger.info(
                f"  活跃代币数据时效: {active_min_elapsed:.0f}s~{active_max_elapsed:.0f}s"
            )
        elif len(active_symbols) > 0:
            logger.info("  活跃代币数据时效: 无数据")
        
        logger.info(f"  重连次数: {self._reconnect_count} (无限重连)")
        logger.info("=" * 60)
    
    async def _reconnect_websocket(self):
        """
        重连WebSocket（带指数退避策略，无限重连）
        
        断开并重新连接交易所WebSocket，重新订阅所有代币
        
        重连策略：
        - 使用指数退避：2s → 4s → 8s → 16s → 32s → 60s（最大，之后保持60秒）
        - 无限重连：不限制重连次数，直到连接成功
        """
        if self._is_reconnecting:
            logger.warning("⏳ 已有重连任务正在执行，跳过本次重连")
            return
        
        self._is_reconnecting = True
        self._reconnect_count += 1
        self._total_reconnect_count += 1  # 🔥 累计总重连次数
        
        # 🔥 更新UI显示重连次数
        if self.ui:
            self.ui.update_reconnect_count(self._total_reconnect_count)
        
        # 🔥 计算指数退避延迟
        # 策略：2^attempt，但限制最大延迟
        backoff_delay = min(
            self._reconnect_base_delay * (2 ** (self._reconnect_count - 1)),
            self._reconnect_max_delay
        )
        
        try:
            logger.warning(
                f"🔄 开始重连 WebSocket (第 {self._reconnect_count} 次，"
                f"等待 {backoff_delay:.1f}秒后重试，无限重连)..."
            )
            
            # 🔥 指数退避：等待一段时间再重连
            await asyncio.sleep(backoff_delay)
            
            # 1. 断开现有连接
            try:
                await self.adapter.disconnect()
                logger.info("✅ 已断开WebSocket连接")
            except Exception as e:
                logger.error(f"断开连接失败: {e}")
            
            # 2. 重新连接
            try:
                await self.adapter.connect()
                logger.info("✅ WebSocket重新连接成功")
            except Exception as e:
                logger.error(f"❌ 重新连接失败: {e}")
                self._is_reconnecting = False
                return
            
            # 3. 重新订阅所有代币
            try:
                symbols_to_resubscribe = list(self.virtual_grids.keys())
                logger.info(f"📡 重新订阅 {len(symbols_to_resubscribe)} 个代币...")
                
                # 将字符串列表转换为字典列表格式（_create_virtual_grids期望的格式）
                markets_to_resubscribe = [{'symbol': symbol} for symbol in symbols_to_resubscribe]
                
                # 重新创建虚拟网格（这会重新订阅）
                await self._create_virtual_grids(markets_to_resubscribe)
                
                logger.info("✅ 重新订阅完成")
                
                # 清除旧的数据时间戳
                self._last_data_time.clear()
                
                # 🔥 重连成功，重置重连计数
                successful_attempt = self._reconnect_count
                self._reconnect_count = 0
                
                logger.info(f"✅ WebSocket重连完成 (第 {successful_attempt} 次尝试成功)")
                
            except Exception as e:
                logger.error(f"❌ 重新订阅失败: {e}")
                raise  # 重新订阅失败，继续重试
                
        except Exception as e:
            logger.error(f"❌ WebSocket重连失败: {e}", exc_info=True)
            # 不重置 _is_reconnecting，让调用者决定是否继续重试
        finally:
            self._is_reconnecting = False
