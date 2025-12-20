"""
分段套利系统统一调度器（重构版）

模式总览：
- SEG-GRID    ：分段网格模式，价差扩大/缩小时逐步开平仓
- SEG-SCALP   ：分段剥头皮模式，达到触发格子后锁定，止盈退出
- SEG-GRID+   ：分段网格拆单模式，支持单格子多笔累积

核心改进：
1. 使用统一决策引擎（总量驱动算法）
2. 支持多交易对独立配置
3. 简化的调度逻辑
4. 关键子模块拆分：
   - orchestrator_bootstrap：负责初始化/连接/配置加载
   - orchestrator_ui_controller：负责 UI 汇总与展示
   - spread_pipeline：承担行情采集与正反向价差流水线
   - risk_control_utils：封装价格稳定/流动性/双限价避让工具
   - reduce_only_probe_service：提供整点 reduce-only 探测与恢复
"""

import asyncio
import time
import logging
import time
import re
from typing import Dict, Optional, Set, List, Tuple, Any
from collections import defaultdict
from pathlib import Path
from decimal import Decimal, InvalidOperation
from dataclasses import asdict
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
import yaml

from ..config.symbol_config import SegmentedConfigManager
from ..config.monitor_config import ConfigManager as MonitorConfigManager
from ..config.debug_config import DebugConfig
from ..config.unified_config_manager import UnifiedConfigManager
from ..config.arbitrage_config import RiskControlConfig, ExecutionConfig, ExchangeFeeConfig
from ..config.multi_leg_pairs_config import MultiLegPairsConfigManager, MultiLegPairConfig
from ..config.multi_exchange_config import (
    MultiExchangeArbitrageConfigManager,
    TradingPair,
)
# 🔥 使用V2统一决策引擎（总量驱动算法）
from ..decision.unified_decision_engine import UnifiedDecisionEngine
from ..execution.arbitrage_executor import ArbitrageExecutor, ExecutionRequest, ExecutionResult
from ..analysis.spread_calculator import SpreadCalculator, SpreadData
from ..guards.reduce_only_guard import ReduceOnlyGuard
from ..data.data_receiver import DataReceiver
from ..data.data_processor import DataProcessor
from ..display.ui_manager import UIManager, UIMode
from ..display.realtime_scroller import RealtimeScroller
from ..utils.orchestrator_utils import ThrottledLogger, LiquidityFailureLogger
from ..utils.risk_control_utils import RiskControlUtils
from .orchestrator_ui_controller import OrchestratorUIController
from .spread_pipeline import SpreadPipeline
from .reduce_only_probe_service import ReduceOnlyProbeService
from .orchestrator_bootstrap import OrchestratorBootstrap
from ..models import FundingRateData, PositionSegment, SegmentedPosition
from ..risk_control.global_risk_controller import GlobalRiskController
from .debug_state_printer import DebugStatePrinter
from ..state.symbol_state_manager import SymbolStateManager

from core.adapters.exchanges.interface import ExchangeInterface
from core.adapters.exchanges.models import OrderBookData

from core.adapters.exchanges.utils.setup_logging import LoggingConfig

# 调度层默认降到 WARNING，减少大行情下的日志格式化与写入开销
logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file='unified_orchestrator.log',
    console_formatter=None,  # 🔥 不输出到终端
    file_formatter='detailed',
    level=logging.WARNING
)
# 🔥 额外确保不传播到父logger，防止终端抖动
logger.propagate = False


class UnifiedOrchestrator:
    """统一调度器（支持多交易对独立配置）"""
    
    def __init__(
        self,
        segmented_config_path: Optional[Path] = None,
        monitor_config_path: Optional[Path] = None,
        debug_config: Optional[DebugConfig] = None
    ):
        """
        初始化统一调度器
        
        Args:
            segmented_config_path: 分段配置文件路径
            monitor_config_path: 监控配置文件路径
            debug_config: Debug配置
        """
        # 加载配置
        self.config_manager = SegmentedConfigManager(segmented_config_path)
        self.monitor_config_manager = MonitorConfigManager(monitor_config_path)
        self.monitor_config = self.monitor_config_manager.get_config()
        self.debug = debug_config or DebugConfig.create_production()
        self.symbol_spreads: Dict[str, List[SpreadData]] = {}
        # 持仓一致性日志节流（仅在变更或超过间隔时打印）
        self._alignment_log_interval: float = 120.0
        self._last_alignment_snapshot: Dict[str, str] = {}
        self._last_alignment_log_time: Dict[str, float] = {}
        self._last_alignment_ui_data: Optional[Dict[str, Any]] = None
        self._unified_execution_config_manager: Optional[UnifiedConfigManager] = None
        
        # 🔥 加载多腿套利配置
        self.multi_leg_pairs_manager = MultiLegPairsConfigManager()
        self.multi_leg_pairs: List[MultiLegPairConfig] = self.multi_leg_pairs_manager.get_pairs()
        if self.multi_leg_pairs:
            logger.info(f"✅ [统一调度] 加载多腿套利配置: {len(self.multi_leg_pairs)} 对")
            for pair in self.multi_leg_pairs:
                logger.info(f"  - {pair.pair_id}: {pair.description}")
        
        # 🔥 加载多交易所套利配置
        self.multi_exchange_config_manager = MultiExchangeArbitrageConfigManager()
        self.multi_exchange_pairs: List[TradingPair] = self.multi_exchange_config_manager.get_pairs()
        self.multi_exchange_symbols: Set[str] = set()
        if self.multi_exchange_pairs:
            for pair in self.multi_exchange_pairs:
                self.config_manager.register_config_alias(pair.trading_pair_id, pair.symbol)
                self.multi_exchange_symbols.add(pair.normalized_symbol())
            logger.info("✅ [统一调度] 加载多交易所套利配置: %d 对", len(self.multi_exchange_pairs))
        
        # 验证配置
        if not self.monitor_config_manager.validate():
            raise ValueError("监控配置验证失败")
        
        logger.info("✅ [统一调度] 配置加载完成")
        
        # 创建队列
        self.orderbook_queue = asyncio.Queue(maxsize=self.monitor_config.orderbook_queue_size)
        self.ticker_queue = asyncio.Queue(maxsize=self.monitor_config.ticker_queue_size)
        
        # 初始化交易所适配器
        self.exchange_adapters: Dict[str, ExchangeInterface] = {}
        self.bootstrapper = OrchestratorBootstrap(self)
        self._root_config: Optional[Dict[str, Any]] = None
        self.bootstrapper.init_exchange_adapters()
        self.reduce_only_guard = ReduceOnlyGuard(ZoneInfo("Asia/Shanghai"))
        
        # 初始化数据分析模块
        self.spread_calculator = SpreadCalculator(self.debug)
        
        # 🔥 初始化错误避让控制器
        from ..risk_control.error_backoff_controller import ErrorBackoffController
        self.error_backoff_controller = ErrorBackoffController()
        logger.info("✅ [统一调度] 错误避让控制器已初始化")
        
        # 🔥 初始化V2统一决策引擎（总量驱动 + 剥头皮状态机）
        self.decision_engine = UnifiedDecisionEngine(
            config_manager=self.config_manager
        )
        # 注入避让控制器
        self.decision_engine._backoff_controller = self.error_backoff_controller
        logger.info("✅ [统一调度] V2统一决策引擎已初始化（总量驱动算法）")
        
        # 🔥 将避让控制器注入到交易所适配器
        for exchange_name, adapter in self.exchange_adapters.items():
            # 适配器自身
            if hasattr(adapter, '_backoff_controller'):
                adapter._backoff_controller = self.error_backoff_controller
                logger.info(f"✅ [统一调度] 错误避让控制器已注入到 {exchange_name}")
                # 如果是 lighter，注册局部重启钩子（重建 REST/WS，保留缓存）
                if exchange_name.lower() == "lighter" and hasattr(adapter, "restart_connections"):
                    try:
                        self.error_backoff_controller.set_restart_hook(
                            exchange_name,
                            adapter.restart_connections
                        )
                        logger.info("✅ [统一调度] lighter 已注册局部重启钩子 (21104 时重建 REST/WS)")
                    except Exception as e:
                        logger.warning(f"⚠️ [统一调度] 注册 lighter 重启钩子失败: {e}")

            # Lighter REST 层
            if hasattr(adapter, '_rest') and adapter._rest:
                rest = adapter._rest
                if hasattr(rest, '_backoff_controller'):
                    rest._backoff_controller = self.error_backoff_controller
                    logger.info(f"✅ [统一调度] 错误避让控制器已注入到 {exchange_name}.rest")

                # Lighter WebSocket 层
                if hasattr(rest, '_websocket') and rest._websocket:
                    ws = rest._websocket
                    if hasattr(ws, '_backoff_controller'):
                        ws._backoff_controller = self.error_backoff_controller
                        logger.info(f"✅ [统一调度] 错误避让控制器已注入到 {exchange_name}.websocket")

            # 若适配器本身就包含 websocket（如直接使用 REST 适配器实例）
            if hasattr(adapter, '_websocket') and adapter._websocket:
                ws = adapter._websocket
                if hasattr(ws, '_backoff_controller'):
                    ws._backoff_controller = self.error_backoff_controller
                    logger.info(f"✅ [统一调度] 错误避让控制器已注入到 {exchange_name}.websocket")
        
        # 初始化风险控制器
        risk_config = self._load_risk_control_config()
        self.risk_controller = GlobalRiskController(
            risk_config=risk_config,
            exchange_adapters=self.exchange_adapters
        )
        
        # 单套利对状态管理（运行/等待）
        self.symbol_state_manager = SymbolStateManager()
        
        # 初始化执行器
        execution_config = self._load_execution_module_config()
        # 获取monitor_only配置
        monitor_only = self.config_manager.get_system_mode().get('monitor_only', True)
        self.monitor_only_mode = monitor_only
        self.executor = ArbitrageExecutor(
            execution_config=execution_config,
            exchange_adapters=self.exchange_adapters,
            monitor_only=monitor_only,
            is_segmented_mode=True,  # 🔥 分段模式：禁用轮次间隔，允许快速拆单补仓
            reduce_only_guard=self.reduce_only_guard,
            symbol_state_manager=self.symbol_state_manager,
        )
        
        # 初始化展示组件
        self.scroller = RealtimeScroller()
        
        # 初始化数据模块
        self.data_receiver = DataReceiver(
            self.orderbook_queue,
            self.ticker_queue,
            self.debug
        )
        
        self.data_processor = DataProcessor(
            self.orderbook_queue,
            self.ticker_queue,
            self.debug,
            scroller=self.scroller
        )
        self.executor.set_live_price_resolver(self._resolve_live_price_from_cache)
        
        # 初始化UI
        self.ui_manager = UIManager(self.debug, self.scroller)
        self.ui_controller = OrchestratorUIController(self, logger)
        self.spread_pipeline = SpreadPipeline(self)
        self.reduce_only_probe_service = ReduceOnlyProbeService(self)
        self.debug_cli_printer: Optional[DebugStatePrinter] = None
        
        # 运行状态
        self.running = False
        self.loop_interval = 0.1  # 主循环间隔（秒）
        
        # 数据新鲜度配置
        self.data_freshness_seconds = self.config_manager.get_system_mode().get('data_freshness_seconds', 3.0)
        
        # Ticker日志控制
        self._missing_ticker_logged: Set[Tuple[str, str]] = set()
        
        # 决策心跳日志控制
        self.decision_log_interval: float = 60.0  # 默认每分钟输出一次
        self._last_decision_log_time: float = 0.0
        self._decision_snapshots: Dict[str, Dict[str, Any]] = {}
        
        # 🔥 状态汇总日志控制
        self.status_summary_interval: float = 60.0  # 每60秒输出一次状态汇总
        self._last_status_summary_time: float = 0.0
        self._liquidity_failure_logger = LiquidityFailureLogger(logger)
        self._throttled_logger = ThrottledLogger(logger)
        self._persistence_log_records: Dict[str, Tuple[float, float]] = {}
        self._open_intent_log_records: Dict[str, Tuple[float, float]] = {}
        self.risk_utils = RiskControlUtils(self)
        # 🔐 组合级执行锁，防止同一交易所组合重复触发
        self._pending_open_pairs: Set[str] = set()
        self._pending_close_symbols: Set[str] = set()
        self._pending_open_lock = asyncio.Lock()
        self._pending_close_lock = asyncio.Lock()
        
        # 🔥 根据决策引擎类型设置UI模式
        self._determine_ui_mode()
        
        logger.info("✅ [统一调度] 统一调度器初始化完成")
    
    def _determine_ui_mode(self):
        """
        根据决策引擎类型确定UI模式
        
        逻辑：
        - 如果使用 UnifiedDecisionEngine (V2) -> SEGMENTED_GRID 模式
        - 其他情况保持原有行为（ARBITRAGE_V3 或 MONITOR）
        """
        from ..display.ui_manager import UIMode
        
        # 检查决策引擎类型
        if hasattr(self, 'decision_engine') and \
           self.decision_engine.__class__.__name__ == 'UnifiedDecisionEngine':
            # 🔥 分段网格模式
            self.ui_manager.set_ui_mode(UIMode.SEGMENTED_GRID)
            logger.info("🎨 [统一调度] UI模式: 分段网格模式 (SEGMENTED_GRID)")
        else:
            # 保持原有逻辑（V3基础模式或监控模式）
            # 这里不需要额外设置，已经在 set_v3_mode 中处理
            logger.info(f"🎨 [统一调度] UI模式: {'V3基础模式' if self.ui_manager.is_v3_mode else '监控模式'}")
    
    @staticmethod
    def _build_symbol_aliases(symbol: Optional[str]) -> List[str]:
        """
        针对不同交易所的交易对命名差异，生成若干别名用于缓存匹配。
        """
        if not symbol:
            return []
        candidates = {
            symbol,
            symbol.upper(),
            symbol.lower(),
            symbol.replace("-", "_"),
            symbol.replace("_", "-"),
            symbol.replace("/", "-"),
            symbol.replace("/", "_"),
        }
        return [item for item in candidates if item]

    def _resolve_live_price_from_cache(
        self,
        exchange: str,
        symbol: str,
        is_buy: bool,
    ) -> Optional[Decimal]:
        """
        提供给执行器的实时盘口价格解析器，优先读取本地WS缓存。
        """
        if not getattr(self, "data_processor", None):
            return None

        exchange_candidates = [exchange, exchange.upper(), exchange.lower()]
        symbol_candidates = self._build_symbol_aliases(symbol)

        for exchange_key in dict.fromkeys(exchange_candidates):
            if not exchange_key:
                continue
            for symbol_key in symbol_candidates:
                orderbook = self.data_processor.get_orderbook(exchange_key, symbol_key)
                if not orderbook:
                    continue
                side = getattr(orderbook, "best_ask", None) if is_buy else getattr(orderbook, "best_bid", None)
                price = getattr(side, "price", None) if side else None
                if price in (None, 0):
                    continue
                try:
                    return Decimal(str(price))
                except (InvalidOperation, ValueError, TypeError):
                    continue
        return None

    def _build_live_spread_from_request(
        self,
        request: "ExecutionRequest",
    ) -> Optional[SpreadData]:
        if not request:
            return None
        exchange_buy = request.exchange_buy
        exchange_sell = request.exchange_sell
        buy_symbol = request.buy_symbol or request.symbol
        sell_symbol = request.sell_symbol or request.symbol
        if not exchange_buy or not exchange_sell or not buy_symbol or not sell_symbol:
            return None
        buy_ob = self.data_processor.get_orderbook(
            exchange_buy,
            buy_symbol,
            max_age_seconds=self.data_freshness_seconds,
        )
        sell_ob = self.data_processor.get_orderbook(
            exchange_sell,
            sell_symbol,
            max_age_seconds=self.data_freshness_seconds,
        )
        if not buy_ob or not sell_ob:
            return None
        best_ask = getattr(buy_ob, "best_ask", None)
        best_bid = getattr(sell_ob, "best_bid", None)
        if not best_ask or not best_bid:
            return None
        price_buy = self.executor._to_decimal_value(getattr(best_ask, "price", None))
        price_sell = self.executor._to_decimal_value(getattr(best_bid, "price", None))
        if price_buy <= Decimal("0") or price_sell <= Decimal("0"):
            return None
        size_buy = self.executor._to_decimal_value(getattr(best_ask, "size", None))
        size_sell = self.executor._to_decimal_value(getattr(best_bid, "size", None))
        spread_abs = price_sell - price_buy
        spread_pct = (
            float((spread_abs / price_buy) * Decimal("100"))
            if price_buy > Decimal("0")
            else 0.0
        )
        return SpreadData(
            symbol=request.symbol,
            exchange_buy=exchange_buy,
            exchange_sell=exchange_sell,
            price_buy=price_buy,
            price_sell=price_sell,
            size_buy=size_buy,
            size_sell=size_sell,
            spread_abs=spread_abs,
            spread_pct=spread_pct,
            buy_symbol=buy_symbol,
            sell_symbol=sell_symbol,
        )

    async def _validate_retry_preconditions(
        self,
        request: "ExecutionRequest",
    ) -> bool:
        """
        供执行器重试前调用，复用三道门槛（价差→稳定性→流动性）。
        """
        spread = self._build_live_spread_from_request(request)
        if not spread:
            logger.warning(
                "⛔️ [重试门槛] %s: 无最新盘口数据，放弃重新挂单",
                request.symbol,
            )
            return False

        symbol = (request.symbol or "").upper()
        action = "开仓" if request.is_open else "平仓"
        funding_rate = None
        try:
            funding_rate = self._get_funding_rate_data(
                symbol,
                spread.exchange_buy,
                spread.exchange_sell,
            )
        except Exception:
            funding_rate = None

        if request.is_open:
            should_open, open_quantity = await self.decision_engine.should_open(
                symbol,
                spread,
                funding_rate,
            )
            if not should_open or open_quantity <= Decimal("0"):
                # 🔥 检查是否检测到反向开仓（实为平仓信号）
                if getattr(self.decision_engine, '_reverse_open_detected', False):
                    self.decision_engine._reverse_open_detected = False  # 重置标记
                    logger.info(f"🔄 [重试门槛] {symbol}: 反向开仓被拦截，但这是平仓信号，允许继续")
                    # 不终止补单，让平仓逻辑处理
                    return True
                logger.info(
                    "⛔️ [重试门槛] %s: 价差已不满足开仓条件，终止补单",
                    symbol,
                )
                return False
        else:
            should_close, close_quantity, reason, _ = await self.decision_engine.should_close(
                symbol,
                spread,
                funding_rate,
            )
            if not should_close or close_quantity <= Decimal("0"):
                logger.info(
                    "⛔️ [重试门槛] %s: 平仓条件未满足（%s），终止补单",
                    symbol,
                    reason or "无原因",
                )
                return False

        if not self._passes_price_stability(symbol, spread, action=action):
            logger.info(
                "⛔️ [重试门槛] %s: %s价格稳定性未通过，终止补单",
                symbol,
                action,
            )
            return False

        if (
            not self.executor.monitor_only
            and self._should_enforce_orderbook_liquidity(symbol)
        ):
            min_ob_qty = self._get_min_orderbook_quantity(symbol)
            target_quantity = self.executor._to_decimal_value(
                request.quantity or Decimal("0")
            )
            if request.is_open:
                if target_quantity <= Decimal("0"):
                    target_quantity = self.executor._to_decimal_value(open_quantity)
            else:
                target_quantity = max(
                    target_quantity,
                    self.executor._to_decimal_value(close_quantity),
                )
            legs = [
                {
                    "exchange": spread.exchange_buy,
                    "symbol": spread.buy_symbol or symbol,
                    "quantity": target_quantity,
                    "is_buy": True,
                    "desc": "开仓买入腿" if request.is_open else "平仓买回腿",
                    "min_quantity": min_ob_qty,
                },
                {
                    "exchange": spread.exchange_sell,
                    "symbol": spread.sell_symbol or symbol,
                    "quantity": target_quantity,
                    "is_buy": False,
                    "desc": "开仓卖出腿" if request.is_open else "平仓卖出腿",
                    "min_quantity": min_ob_qty,
                },
            ]
            liquidity_ok, failure_detail = self._verify_orderbook_liquidity(
                symbol,
                legs,
                action=action,
            )
            if not liquidity_ok:
                logger.warning(
                    "⛔️ [重试门槛] %s: %s 对手盘不足，终止补单",
                    symbol,
                    failure_detail,
                )
                return False

        return True

    def _load_execution_module_config(self) -> ExecutionConfig:
        """
        载入执行层配置（含下单模式、双限价开关等）
        
        优先读取统一配置文件 `config/arbitrage/arbitrage_unified.yaml`，
        仅当文件缺失或解析失败时才退回默认配置，确保分段模式也能
        复用同一套执行参数。
        """
        config_path = Path("config/arbitrage/arbitrage_unified.yaml")
        if not config_path.exists():
            logger.warning(
                "⚠️ [统一调度] 未找到统一执行配置文件 (%s)，"
                "将使用默认执行配置（市价优先）",
                config_path
            )
            return ExecutionConfig()
        
        try:
            self._unified_execution_config_manager = UnifiedConfigManager(
                config_path=config_path
            )
            unified_config = self._unified_execution_config_manager.get_unified_config()
            exec_config = unified_config.execution
            dual_limit_flag = getattr(
                exec_config.order_execution,
                "enable_dual_limit_mode",
                False
            )
            logger.info(
                "✅ [统一调度] 执行层配置加载完成: dual_limit=%s, order_modes=%d",
                "ON" if dual_limit_flag else "OFF",
                len(exec_config.exchange_order_modes)
            )
            return exec_config
        except Exception as exc:
            logger.warning(
                "⚠️ [统一调度] 读取统一执行配置失败，改用默认执行配置: %s",
                exc,
                exc_info=True
            )
            return ExecutionConfig()
    
    def _load_risk_control_config(self) -> RiskControlConfig:
        """从配置文件加载全局风险控制参数（如余额阈值）"""
        risk_config = RiskControlConfig()
        
        data = self._load_root_config()
        rc_data = data.get('risk_control', {})
        bm_data = rc_data.get('balance_management', {})
        
        balance_cfg = risk_config.balance_management
        balance_cfg.min_balance_warning = float(
            bm_data.get('min_balance_warning', balance_cfg.min_balance_warning)
        )
        balance_cfg.min_balance_close_position = float(
            bm_data.get('min_balance_close_position', balance_cfg.min_balance_close_position)
        )
        balance_cfg.check_interval = int(
            bm_data.get('check_interval', balance_cfg.check_interval)
        )
        
        return risk_config
    
    def _load_decision_settings(self) -> Dict[str, Any]:
        """加载决策相关配置（手续费、是否扣费等）"""
        data = self._load_root_config()
        decision_data = data.get('decision', {})
        settings = {
            'deduct_fees': bool(decision_data.get('deduct_fees', True)),
            'exchange_fee_config': {}
        }
        
        fee_data: Dict[str, Any] = decision_data.get('exchange_fee_config', {})
        for exchange, cfg in fee_data.items():
            if not isinstance(cfg, dict):
                continue
            settings['exchange_fee_config'][exchange] = ExchangeFeeConfig(
                limit_fee_rate=float(cfg.get('limit_fee_rate', ExchangeFeeConfig().limit_fee_rate)),
                market_fee_rate=float(cfg.get('market_fee_rate', ExchangeFeeConfig().market_fee_rate))
            )
        
        return settings
    
    def _load_root_config(self) -> Dict[str, Any]:
        """读取分段配置文件的原始数据并缓存"""
        if self._root_config is not None:
            return self._root_config
        
        config_path = self.config_manager.config_path
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self._root_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning(f"⚠️ [统一调度] 未找到分段配置文件: {config_path}")
            self._root_config = {}
        except Exception as exc:
            logger.warning(f"⚠️ [统一调度] 读取配置失败: {exc}")
            self._root_config = {}
        
        return self._root_config
    
    async def start(self):
        """启动调度器"""
        logger.info("🚀 [统一调度] 启动调度器...")
        
        # 检查交易所适配器
        if not self.exchange_adapters:
            logger.error("❌ [统一调度] 没有可用的交易所适配器，无法启动")
            raise RuntimeError("没有可用的交易所适配器")
        
        logger.info(f"✅ [统一调度] 已加载 {len(self.exchange_adapters)} 个交易所适配器: {list(self.exchange_adapters.keys())}")
        
        # 启动风险控制器
        await self.risk_controller.start()
        logger.info("✅ [统一调度] 风险控制器已启动")
        
        # 启动数据处理器
        await self.data_processor.start()
        logger.info("✅ [统一调度] 数据处理器已启动")
        
        # 连接所有交易所并订阅数据
        await self.bootstrapper.connect_all_exchanges()
        logger.info("✅ [统一调度] 交易所连接和订阅完成")
        
        # 🔥 初始化WebSocket订单追踪（实盘模式）
        if not self.monitor_only_mode:
            logger.info("📡 [统一调度] 初始化WebSocket订单追踪...")
            await self.executor.initialize_websocket_subscriptions()
            logger.info("✅ [统一调度] WebSocket订单追踪初始化完成")
        else:
            logger.info("🔍 [统一调度] 监控模式，跳过WebSocket订单追踪初始化")
        
        debug_cli_enabled = getattr(self.monitor_config, "debug_cli_mode", False)
        if debug_cli_enabled:
            logger.info("🛠️ [统一调度] Debug CLI 模式启用，跳过富UI渲染")
            self.ui_update_task = None
        else:
            self.ui_manager.start(refresh_rate=5)
            if self.ui_manager.ui_mode != UIMode.SEGMENTED_GRID:
                self.ui_manager.set_v3_mode(True, self.monitor_only_mode)
            else:
                self.ui_manager.is_v3_mode = True
                self.ui_manager.monitor_only_mode = self.monitor_only_mode
            self.ui_manager.update_config({
                'exchanges': self.monitor_config.exchanges,
                'symbols': self.monitor_config.symbols,
                'multi_leg_symbols': [
                    pair.pair_id for pair in self.multi_leg_pairs
                ] if self.multi_leg_pairs else []
            })
            logger.info("✅ [统一调度] UI管理器已启动")
            
            self.ui_update_task = asyncio.create_task(
                self.ui_manager.update_loop(self.monitor_config.ui_refresh_interval_ms)
            )
            logger.info("✅ [统一调度] UI渲染循环已启动")
        
        # UI数据更新循环 / Debug CLI
        self.ui_controller.start(enable_ui_render_loop=not debug_cli_enabled)
        if debug_cli_enabled:
            self.debug_cli_printer = DebugStatePrinter(
                self,
                interval_seconds=getattr(
                    self.monitor_config, "debug_cli_interval_seconds", 1.0
                ),
            )
            self.debug_cli_printer.start()
        logger.info("✅ [统一调度] UI数据控制器已启动")
        
        # 3. Reduce-only探测服务
        self.reduce_only_probe_service.start()
        
        # 启动主循环
        self.running = True
        logger.info("✅ [统一调度] 分段套利系统已启动")
        await self._main_loop()
    
    async def check_has_positions(self) -> bool:
        """
        检查是否有未平仓位
        
        Returns:
            bool: True表示有持仓，False表示无持仓
        """
        try:
            positions = self.decision_engine.positions
            
            # 检查是否有任何持仓
            for symbol, position in positions.items():
                if position.is_open and position.total_quantity > 0:
                    return True
            
            return False
        except Exception as e:
            logger.error(f"❌ [统一调度] 检查持仓失败: {e}")
            return False
    
    async def emergency_close_all_positions(self):
        """
        紧急平仓所有持仓（市价单）
        
        用于系统退出时的风险控制
        """
        try:
            positions = self.decision_engine.positions
            
            if not positions:
                logger.info("ℹ️  [紧急平仓] 无持仓需要平仓")
                return
            
            close_tasks = []
            
            for symbol, position in positions.items():
                if not position.is_open or position.total_quantity <= 0:
                    continue
                
                logger.info(
                    f"🔄 [紧急平仓] {symbol}: "
                    f"数量={position.total_quantity}, "
                    f"买入方={position.exchange_buy}, "
                    f"卖出方={position.exchange_sell}"
                )
                
                # 获取当前价差数据
                spread_data = self.data_processor.get_latest_spread(symbol)
                if not spread_data:
                    logger.warning(f"⚠️  [紧急平仓] {symbol}: 无法获取价差数据，跳过")
                    continue
                
                # 创建平仓请求（反向交易）
                from ..execution.arbitrage_executor import ExecutionRequest
                buy_symbol = position.sell_symbol or symbol
                sell_symbol = position.buy_symbol or symbol
                
                # 🔥 获取完整盘口数据
                orderbook_buy = self.data_processor.get_orderbook(position.exchange_sell, buy_symbol)
                orderbook_sell = self.data_processor.get_orderbook(position.exchange_buy, sell_symbol)

                emergency_grid_level = self.decision_engine.get_current_segments(symbol)
                grid_threshold_pct = self._resolve_grid_threshold_pct(symbol, emergency_grid_level)
                
                close_request = ExecutionRequest(
                    symbol=symbol,
                    exchange_buy=position.exchange_sell,  # 🔥 平仓时反向
                    exchange_sell=position.exchange_buy,  # 🔥 平仓时反向
                    price_buy=spread_data.price_sell,
                    price_sell=spread_data.price_buy,
                    quantity=position.total_quantity,
                    is_open=False,
                    spread_data=spread_data,
                    buy_symbol=buy_symbol,
                    sell_symbol=sell_symbol,
                    grid_action="close",
                    grid_level=emergency_grid_level or None,
                    grid_threshold_pct=grid_threshold_pct,
                    limit_price_offset_buy=self._get_limit_price_offset_for_symbol(buy_symbol),
                    limit_price_offset_sell=self._get_limit_price_offset_for_symbol(sell_symbol),
                    min_exchange_order_qty=self._build_min_exchange_order_qty_map(symbol),
                    orderbook_buy_ask=spread_data.price_sell if orderbook_buy and orderbook_buy.best_ask else None,
                    orderbook_buy_bid=orderbook_buy.best_bid.price if orderbook_buy and orderbook_buy.best_bid else None,
                    orderbook_sell_ask=orderbook_sell.best_ask.price if orderbook_sell and orderbook_sell.best_ask else None,
                    orderbook_sell_bid=spread_data.price_buy if orderbook_sell and orderbook_sell.best_bid else None,
                )
                
                # 异步执行平仓
                task = self.executor.execute_arbitrage(close_request)
                close_tasks.append((symbol, task))
            
            # 等待所有平仓完成
            if close_tasks:
                logger.info(f"⏳ [紧急平仓] 等待 {len(close_tasks)} 个持仓平仓...")
                
                for symbol, task in close_tasks:
                    try:
                        result = await task
                        if result.success:
                            logger.info(f"✅ [紧急平仓] {symbol}: 平仓成功")
                            # 记录平仓
                            await self.decision_engine.record_close(
                                symbol=symbol,
                                quantity=position.total_quantity,
                                spread_data=spread_data,
                                reason="系统退出紧急平仓"
                            )
                        else:
                            logger.error(f"❌ [紧急平仓] {symbol}: 平仓失败 - {result.error_message}")
                    except Exception as e:
                        logger.error(f"❌ [紧急平仓] {symbol}: 平仓异常 - {e}")
                
                logger.info("✅ [紧急平仓] 所有平仓操作已完成")
            
        except Exception as e:
            logger.error(f"❌ [紧急平仓] 批量平仓失败: {e}", exc_info=True)
            raise
    
    async def stop(self):
        """停止调度器"""
        logger.info("🛑 [统一调度] 停止调度器...")
        self.running = False
        
        # 停止数据处理器和风险控制器
        await self.data_processor.stop()
        await self.risk_controller.stop()
        
        # 停止UI更新任务
        if hasattr(self, 'ui_update_task') and self.ui_update_task:
            self.ui_update_task.cancel()
            try:
                await self.ui_update_task
            except asyncio.CancelledError:
                pass
            self.ui_update_task = None
        
        if self.debug_cli_printer:
            await self.debug_cli_printer.stop()
            self.debug_cli_printer = None
        
        # 停止UI
        if not getattr(self.monitor_config, "debug_cli_mode", False):
            self.ui_manager.stop()
        await self.ui_controller.stop()
        await self.reduce_only_probe_service.stop()
        
        # 断开所有交易所
        await self.bootstrapper.disconnect_all_exchanges()
        
        logger.info("✅ [统一调度] 调度器已停止")
    
    async def _main_loop(self):
        """主循环：处理套利决策和执行"""
        logger.info("▶️  [统一调度] 主循环启动")
        
        try:
            while self.running:
                try:
                    # 检查风险控制状态
                    risk_status = self.risk_controller.get_risk_status()
                    if risk_status.is_paused:
                        await asyncio.sleep(self.loop_interval)
                        continue
                    
                    # 并行处理所有交易对，提升多symbol时的吞吐
                    symbol_tasks = []
                    symbol_list: List[str] = []
                    for symbol in self.monitor_config.symbols:
                        symbol_upper = symbol.upper()
                        if symbol_upper in self.multi_exchange_symbols:
                            continue
                        if not self.config_manager.is_symbol_enabled(symbol_upper):
                            continue
                        symbol_list.append(symbol_upper)
                        symbol_tasks.append(self.spread_pipeline.process_symbol(symbol_upper))

                    if symbol_tasks:
                        results = await asyncio.gather(*symbol_tasks, return_exceptions=True)
                        for symbol, result in zip(symbol_list, results):
                            if isinstance(result, Exception):
                                logger.error(
                                    "❌ [统一调度] 处理 %s 异常: %s",
                                    symbol,
                                    result,
                                    exc_info=result,
                                )
                    
                    # 🔥 处理多腿套利组合
                    if self.multi_leg_pairs:
                        await self._process_multi_leg_pairs()

                    # 🔥 处理多交易所套利组合
                    if self.multi_exchange_pairs:
                        await self._process_trading_pairs()
                    
                    # 🔥 周期性输出状态汇总
                    self._log_status_summary()
                    
                    # 等待下一次循环
                    await asyncio.sleep(self.loop_interval)
                    
                except asyncio.CancelledError:
                    logger.info("⚠️  [统一调度] 主循环收到取消信号")
                    break
                except KeyboardInterrupt:
                    # 🔥 重要：重新抛出 KeyboardInterrupt，让外层捕获
                    logger.info("⚠️  [统一调度] 主循环收到中断信号 (Ctrl+C)")
                    raise
                except Exception as e:
                    logger.error(f"❌ [统一调度] 主循环异常: {e}", exc_info=True)
                    await asyncio.sleep(1)
        except KeyboardInterrupt:
            # 🔥 确保 KeyboardInterrupt 被传播到 start() 方法
            logger.info("⚠️  [统一调度] 主循环正在退出...")
            raise
        finally:
            logger.info("✅ [统一调度] 主循环已停止")
    
    def _should_enforce_orderbook_liquidity(self, symbol: str) -> bool:
        """
        判断是否需要对该symbol启用对手盘深度校验
        """
        try:
            config = self.config_manager.get_config(symbol)
            return bool(
                getattr(config.grid_config, 'require_orderbook_liquidity', False)
            )
        except Exception:
            return False

    def _get_min_orderbook_quantity(self, symbol: str) -> Optional[Decimal]:
        """
        获取配置的盘口最小可用数量门槛
        """
        try:
            config = self.config_manager.get_config(symbol)
            min_qty = getattr(config.grid_config, 'min_orderbook_quantity', None)
            if min_qty is None:
                return None
            return Decimal(str(min_qty))
        except Exception:
            return None

    def _get_limit_price_offset_for_symbol(self, symbol: Optional[str]) -> Optional[Decimal]:
        """
        获取指定交易对的限价偏移配置（绝对价格增量）
        """
        if not symbol:
            return None
        try:
            config = self.config_manager.get_config(symbol)
        except Exception:
            return None
        raw_offset = getattr(config.grid_config, 'limit_price_offset', None)
        if raw_offset in (None, 0):
            return None
        try:
            offset_value = Decimal(str(raw_offset))
        except Exception:
            logger.warning(
                "⚠️ [配置] %s limit_price_offset=%s 无法解析为Decimal，忽略该配置",
                symbol,
                raw_offset
            )
            return None
        if offset_value <= Decimal('0'):
            return None
        return offset_value

    def _resolve_grid_threshold_pct(
        self,
        symbol: Optional[str],
        grid_level: Optional[int]
    ) -> Optional[Decimal]:
        """
        根据配置计算指定网格级别的开仓阈值（百分比）。
        """
        if not symbol or not grid_level or grid_level <= 0:
            return None
        try:
            config = self.config_manager.get_config(symbol)
        except Exception:
            return None
        grid_cfg = getattr(config, "grid_config", None)
        if not grid_cfg:
            return None
        initial = getattr(grid_cfg, "initial_spread_threshold", None)
        step = getattr(grid_cfg, "grid_step", None)
        if initial is None or step is None:
            return None
        try:
            initial_dec = Decimal(str(initial))
            step_dec = Decimal(str(step))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if initial_dec <= Decimal("0"):
            return None
        if step_dec < Decimal("0"):
            return None
        threshold = initial_dec + step_dec * Decimal(grid_level - 1)
        if threshold <= Decimal("0"):
            return None
        # 量化到4位小数，方便显示
        try:
            return threshold.quantize(Decimal("0.0001"))
        except InvalidOperation:
            return threshold

    def _resolve_slippage_pct(
        self,
        symbol: str,
        symbol_config
    ) -> Optional[Decimal]:
        """
        统一解析滑点配置：优先使用套利对自定义值，缺失时回退到执行层默认值。
        """
        raw_value: Optional[Any] = None
        if symbol_config and symbol_config.grid_config.slippage_tolerance is not None:
            raw_value = symbol_config.grid_config.slippage_tolerance
        else:
            raw_value = getattr(
                self.executor.config.order_execution,
                "max_slippage",
                None,
            )
        if raw_value is None:
            return None
        try:
            return Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError):
            logger.warning(
                "⚠️ [配置] %s 的滑点参数无法解析，将忽略该值: %s",
                symbol,
                raw_value,
            )
            return None

    def _record_price_sample(self, symbol: str, spread_data: SpreadData) -> None:
        self.risk_utils.record_price_sample(symbol, spread_data)

    def _reset_price_history(self, symbol: str, spread_data: SpreadData) -> None:
        self.risk_utils.reset_price_history(symbol, spread_data)

    def _passes_price_stability(
        self,
        symbol: str,
        spread_data: SpreadData,
        *,
        action: str
    ) -> bool:
        return self.risk_utils.passes_price_stability(
            symbol,
            spread_data,
            action=action,
        )

    def _passes_local_orderbook_spread(
        self,
        *,
        symbol: str,
        spread_data: SpreadData,
        threshold_pct: Decimal,
    ) -> bool:
        """
        第四道门槛：校验每条实际交易腿的自身 bid-ask 点差是否低于阈值。
        """
        legs = [
            ("买入腿", spread_data.exchange_buy, spread_data.buy_symbol or symbol),
            ("卖出腿", spread_data.exchange_sell, spread_data.sell_symbol or symbol),
        ]
        checked: Set[Tuple[str, str]] = set()
        epsilon = Decimal("0.00000001")

        for desc, exchange, leg_symbol in legs:
            if not exchange or not leg_symbol:
                continue
            key = (exchange.lower(), leg_symbol.upper())
            if key in checked:
                continue
            checked.add(key)

            spread_pct = self._calculate_local_orderbook_spread_pct(
                exchange=exchange,
                symbol=leg_symbol,
            )
            if spread_pct is None:
                self._log_with_throttle(
                    key=f"local_spread_missing:{symbol}:{exchange}:{leg_symbol}",
                    message=(
                        f"⏸️ [V2开仓] {symbol}: {desc} {exchange}/{leg_symbol} "
                        "缺少盘口数据或价差异常，无法计算bid-ask点差，跳过本次信号。"
                    ),
                    level="warning",
                    throttle_seconds=5.0,
                )
                return False

            if (spread_pct - threshold_pct) > epsilon:
                self._log_with_throttle(
                    key=f"local_spread_block:{symbol}:{exchange}:{leg_symbol}",
                    message=(
                        f"⏸️ [V2开仓] {symbol}: {desc} {exchange}/{leg_symbol} "
                        f"自有点差 {float(spread_pct):.4f}% 高于阈值 {float(threshold_pct):.4f}%，"
                        "跳过本次机会。"
                    ),
                    level="info",
                    throttle_seconds=10.0,
                )
                return False

        return True

    def _calculate_local_orderbook_spread_pct(
        self,
        *,
        exchange: str,
        symbol: str,
    ) -> Optional[Decimal]:
        """
        计算指定交易对在某交易所的 bid-ask 点差百分比。
        """
        orderbook = self.data_processor.get_orderbook(
            exchange,
            symbol,
            max_age_seconds=self.data_freshness_seconds,
        )
        if not orderbook or not orderbook.best_ask or not orderbook.best_bid:
            return None

        try:
            ask_price = Decimal(str(orderbook.best_ask.price))
            bid_price = Decimal(str(orderbook.best_bid.price))
        except (InvalidOperation, TypeError, ValueError):
            return None

        if ask_price <= Decimal("0") or bid_price <= Decimal("0"):
            return None

        spread_abs = ask_price - bid_price
        if spread_abs <= Decimal("0"):
            return Decimal("0")

        try:
            return (spread_abs / ask_price) * Decimal("100")
        except (InvalidOperation, ZeroDivisionError):
            return None

    def _is_symbol_market_open(self, symbol: str, base_symbol: Optional[str] = None) -> bool:
        """
        判断符号是否处于可交易时间。
        当前仅对包含 XAU 的国际黄金符号进行周末休市限制：
        - 北京时间周六 06:00 起休市
        - 北京时间周一 07:00 前不开仓
        - 周日全日不可开仓
        """
        check_symbol = base_symbol or symbol
        upper_symbol = (check_symbol or "").upper()
        if "XAU" not in upper_symbol:
            return True

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        weekday = now.weekday()  # Monday=0 ... Sunday=6
        current_time = now.time()
        sat_cutoff = dt_time(hour=5, minute=59)
        mon_cutoff = dt_time(hour=7, minute=0, second=5)

        # 北京时间周六 06:00 之后到周一 07:00 前休市
        if weekday == 5 and current_time >= sat_cutoff:
            return False
        if weekday == 6:
            return False
        if weekday == 0 and current_time < mon_cutoff:
            return False
        return True

    def _log_with_throttle(
        self,
        key: str,
        message: str,
        *,
        level: str = "info",
        throttle_seconds: float = 0.5
    ) -> None:
        self._throttled_logger.log(
            key=key,
            message=message,
            level=level,
            throttle_seconds=throttle_seconds,
        )

    def _should_log_persistence_confirmation(
        self,
        symbol: str,
        action: str,
        spread_pct: float,
        *,
        time_window: float = 2.0,
        spread_epsilon: float = 0.003,
    ) -> bool:
        key = f"{symbol}:{action}"
        now = time.time()
        last_entry = self._persistence_log_records.get(key)
        if last_entry:
            last_spread, last_time = last_entry
            if (now - last_time) < time_window and abs(last_spread - spread_pct) < spread_epsilon:
                return False
        self._persistence_log_records[key] = (spread_pct, now)
        return True

    def _should_log_open_intent(
        self,
        symbol: str,
        spread_pct: float,
        *,
        time_window: float = 1.5,
        spread_epsilon: float = 0.002,
    ) -> bool:
        now = time.time()
        last_entry = self._open_intent_log_records.get(symbol)
        if last_entry:
            last_spread, last_time = last_entry
            if abs(last_spread - spread_pct) < spread_epsilon and (now - last_time) < time_window:
                return False
        self._open_intent_log_records[symbol] = (spread_pct, now)
        return True

    def _should_skip_due_to_dual_limit_backoff(self, symbol: str) -> bool:
        return self.risk_utils.should_skip_due_to_dual_limit_backoff(symbol)

    def _schedule_dual_limit_backoff(self, symbol: str) -> None:
        self.risk_utils.schedule_dual_limit_backoff(symbol)

    def _clear_dual_limit_backoff(self, symbol: str) -> None:
        self.risk_utils.clear_dual_limit_backoff(symbol)
    
    def _log_liquidity_failure_summary(
        self,
        symbol: str,
        *,
        reason: str,
        failure_detail: Optional[Dict[str, Any]],
        base_message: str,
        throttle_seconds: float = 1.0
    ) -> None:
        self._liquidity_failure_logger.log(
            symbol,
            reason=reason,
            failure_detail=failure_detail,
            base_message=base_message,
            throttle_seconds=throttle_seconds,
        )
    
    def _clear_liquidity_failure_summary(self, reason: str, symbol: str) -> None:
        self._liquidity_failure_logger.clear(reason, symbol)
    
    def _verify_orderbook_liquidity(
        self,
        symbol: str,
        legs: List[Dict[str, Any]],
        action: str = "开仓"
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        return self.risk_utils.verify_orderbook_liquidity(symbol, legs, action=action)
    
    def _log_persistence_confirmation(
        self,
        symbol: str,
        action: str,
        spread_pct: float
    ):
        """在实际下单前输出持续性检查结果，帮助验证严格/宽松模式"""
        try:
            config = self.config_manager.get_config(symbol)
        except Exception:
            return
        
        seconds = config.grid_config.spread_persistence_seconds
        if seconds <= 1:
            return
        
        mode = "严格" if config.grid_config.strict_persistence_check else "宽松"
        if not self._should_log_persistence_confirmation(symbol, action, spread_pct):
            return
        logger.info(
            "🛡️ [%s] %s持续性确认(%s/%ds) - 当前价差 %.4f%%",
            symbol,
            action,
            mode,
            seconds,
            spread_pct
        )

    
    async def _process_multi_leg_pairs(self):
        await self.spread_pipeline.process_multi_leg_pairs()

    def _select_reverse_spread(
        self,
        reference: SpreadData,
        candidates: List[SpreadData]
    ) -> SpreadData:
        """在候选列表中寻找 reference 的反向价差，若不存在则构造一份。"""
        for candidate in candidates:
            if self._is_reverse_spread(candidate, reference):
                return candidate
        
        # 🔥 未找到反向价差，使用兜底逻辑（字段交换）
        logger.warning(
            f"⚠️ [价差计算] {reference.symbol}: 未找到反向价差，"
            f"使用字段交换兜底逻辑（可能导致平仓数据不准确）"
        )
        return self._build_reverse_spread(reference)

    @staticmethod
    def _is_reverse_spread(candidate: SpreadData, reference: SpreadData) -> bool:
        """判断 candidate 是否为 reference 的反向价差。"""
        def _norm(value: Optional[str]) -> str:
            return (value or "").lower()

        return (
            _norm(candidate.exchange_buy) == _norm(reference.exchange_sell)
            and _norm(candidate.exchange_sell) == _norm(reference.exchange_buy)
            and _norm(candidate.buy_symbol) == _norm(reference.sell_symbol)
            and _norm(candidate.sell_symbol) == _norm(reference.buy_symbol)
        )

    @staticmethod
    def _build_reverse_spread(spread: SpreadData) -> SpreadData:
        """基于给定价差构造其反向视角的 SpreadData。"""
        return SpreadData(
            symbol=spread.symbol,
            exchange_buy=spread.exchange_sell,
            exchange_sell=spread.exchange_buy,
            price_buy=spread.price_sell,
            price_sell=spread.price_buy,
            size_buy=spread.size_sell,
            size_sell=spread.size_buy,
            spread_abs=-spread.spread_abs,
            spread_pct=-spread.spread_pct,
            buy_symbol=spread.sell_symbol,
            sell_symbol=spread.buy_symbol
        )

    async def _process_trading_pairs(self):
        await self.spread_pipeline.process_trading_pairs()
    
    def _get_funding_rate_data(
        self,
        symbol: str,
        exchange_buy: str,
        exchange_sell: str
    ) -> Optional[FundingRateData]:
        """兼容旧接口：委托 SpreadPipeline 获取资金费率。"""
        return self.spread_pipeline._get_funding_rate_data(symbol, exchange_buy, exchange_sell)
    
    async def _check_and_open(
        self,
        symbol: str,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData],
        *,
        config_symbol: Optional[str] = None
    ):
        """检查并执行开仓（V2总量驱动算法）"""
        try:
            if self.reduce_only_guard.is_pair_blocked(symbol):
                self._log_with_throttle(
                    key=f"reduce_only_open:{symbol}",
                    message=f"⏸️ [V2开仓] {symbol}: 交易所在 reduce-only 模式，仅允许平仓，跳过开仓。",
                    throttle_seconds=60.0,
                )
                return
            if self._should_skip_due_to_dual_limit_backoff(symbol):
                return
            symbol_config = self.config_manager.get_config(symbol)
            slippage_pct = self._resolve_slippage_pct(symbol, symbol_config)
            if not self._is_symbol_market_open(symbol, base_symbol=config_symbol):
                self._log_with_throttle(
                    key=f"market_closed:{symbol}",
                    message=(
                        f"⏸️ [V2开仓] {symbol}: 当前处于休市时间，跳过开仓。"
                        "仅允许reduce-only平仓。"
                    ),
                    # 该模块 logger 级别默认为 WARNING（见文件顶部 setup_logger），
                    # 若这里用 info 会被过滤，导致“休市功能已触发但日志看不到”的错觉。
                    level="warning",
                    throttle_seconds=60.0,
                )
                return
            # 🔥 V2接口：返回(是否开仓, 开仓数量)
            should_open, open_quantity = await self.decision_engine.should_open(
                symbol,
                spread_data,
                funding_rate_data
            )
            
            if not should_open or open_quantity <= Decimal('0'):
                # 🔥 检查是否检测到反向开仓（实为平仓信号）
                if getattr(self.decision_engine, '_reverse_open_detected', False):
                    self.decision_engine._reverse_open_detected = False  # 重置标记
                    logger.info(f"🔄 [{symbol}] 反向开仓被拦截，立即触发平仓检查")
                    # 立即执行平仓检查
                    await self._check_and_close(
                        symbol,
                        spread_data,
                        funding_rate_data
                    )
                return

            if not self._passes_price_stability(symbol, spread_data, action="开仓"):
                return
            
            local_spread_threshold = getattr(
                symbol_config.grid_config,
                "max_local_orderbook_spread_pct",
                None,
            )
            local_spread_threshold_dec: Optional[Decimal] = None
            if local_spread_threshold is not None:
                try:
                    local_spread_threshold_dec = Decimal(str(local_spread_threshold))
                except (InvalidOperation, TypeError, ValueError):
                    logger.warning(
                        "⚠️ [配置] %s: max_local_orderbook_spread_pct=%s 无法解析，已忽略该门槛",
                        symbol,
                        local_spread_threshold,
                    )
                    local_spread_threshold_dec = None
            if (
                local_spread_threshold_dec is not None
                and local_spread_threshold_dec > Decimal("0")
            ):
                if not self._passes_local_orderbook_spread(
                    symbol=symbol,
                    spread_data=spread_data,
                    threshold_pct=local_spread_threshold_dec,
                ):
                    return
            
            # 🔢 当前网格层级（需在日志前计算）
            grid_level = self.decision_engine.get_grid_level(symbol, spread_data.spread_pct)
            if self.symbol_state_manager:
                blocked, state = self.symbol_state_manager.should_block(symbol, grid_level)
                if blocked:
                    reason = state.reason if state else "等待中"
                    logger.info(
                        "⏸️ [V2开仓] %s: 当前处于等待状态，原因=%s，保持跳过 (T%s)",
                        symbol,
                        reason,
                        state.grid_level if state else grid_level,
                    )
                    return
            
            buy_symbol = spread_data.buy_symbol or symbol
            sell_symbol = spread_data.sell_symbol or symbol
            
            # 🔥 获取真实的平仓视角价格（从当前订单簿重新计算，而不是简单对调）
            # 这样日志中显示的平仓价格才是真实的市场价格
            closing_buy_exchange = spread_data.exchange_sell
            closing_sell_exchange = spread_data.exchange_buy
            closing_buy_symbol = sell_symbol
            closing_sell_symbol = buy_symbol
            
            # 🔥 尝试从当前订单簿获取真实的平仓价格
            # 平仓时：在 exchange_sell 买回，在 exchange_buy 卖出
            closing_buy_price = spread_data.price_sell  # 默认值（兜底）
            closing_sell_price = spread_data.price_buy  # 默认值（兜底）
            closing_spread_pct = -spread_data.spread_pct  # 默认值（兜底）
            
            try:
                # 尝试获取当前订单簿数据
                closing_buy_ob = self.data_processor.get_orderbook(
                    closing_buy_exchange,
                    closing_buy_symbol,
                    max_age_seconds=self.data_freshness_seconds
                )
                closing_sell_ob = self.data_processor.get_orderbook(
                    closing_sell_exchange,
                    closing_sell_symbol,
                    max_age_seconds=self.data_freshness_seconds
                )
                
                # 如果订单簿数据可用，使用真实的当前价格
                if closing_buy_ob and closing_buy_ob.best_ask:
                    closing_buy_price = closing_buy_ob.best_ask.price
                if closing_sell_ob and closing_sell_ob.best_bid:
                    closing_sell_price = closing_sell_ob.best_bid.price
                
                # 重新计算平仓价差
                if closing_buy_ob and closing_sell_ob and closing_buy_ob.best_ask and closing_sell_ob.best_bid:
                    closing_spread_abs = closing_sell_price - closing_buy_price
                    closing_spread_pct = float((closing_spread_abs / closing_buy_price) * 100)
            except Exception as e:
                logger.debug(f"[开仓意图] {symbol}: 无法获取实时平仓价格，使用兜底值: {e}")
            
            position = self.decision_engine.get_position(symbol)
            current_qty = position.total_quantity if position else Decimal('0')
            
            buy_offset_str = "0"
            sell_offset_str = "0"
            config_limit_offset = None
            if config_symbol:
                config_limit_offset = self._get_limit_price_offset_for_symbol(config_symbol)
            buy_limit_offset = config_limit_offset or self._get_limit_price_offset_for_symbol(buy_symbol)
            sell_limit_offset = config_limit_offset or self._get_limit_price_offset_for_symbol(sell_symbol)
            if buy_limit_offset:
                buy_offset_str = f"{buy_limit_offset:+.4f}"
            if sell_limit_offset:
                sell_offset_str = f"{sell_limit_offset:+.4f}"
            
            if self._should_log_open_intent(symbol, spread_data.spread_pct):
                open_message = (
                    f"📈 [开仓意图] {symbol} | "
                    f"数量={open_quantity} | "
                    f"当前持仓={current_qty} | "
                    f"买{spread_data.exchange_buy}@{spread_data.price_buy:.2f}(偏移{buy_offset_str}) "
                    f"卖{spread_data.exchange_sell}@{spread_data.price_sell:.2f}(偏移{sell_offset_str}) | "
                    f"开仓价差={spread_data.spread_pct:.4f}% | "
                    f"平仓视角: 买{closing_buy_exchange}@{closing_buy_price:.2f} "
                    f"卖{closing_sell_exchange}@{closing_sell_price:.2f} "
                    f"(价差={closing_spread_pct:.4f}%) | "
                    f"网格T{grid_level}"
                )
                self._log_with_throttle(
                    key=f"open_intent:{symbol}",
                    message=open_message,
                    throttle_seconds=3.0
                )

            # 🔥 详细价差数据 - 仅在DEBUG模式或首次开仓时输出
            if self.debug.is_debug_enabled() or current_qty <= Decimal('0'):
                detail_msg = (
                    "🔀 [开仓详情] "
                    f"{symbol} | 买{spread_data.exchange_buy}/{buy_symbol}@{float(spread_data.price_buy):.2f}(偏移{buy_offset_str}) "
                    f"卖{spread_data.exchange_sell}/{sell_symbol}@{float(spread_data.price_sell):.2f}(偏移{sell_offset_str}) | "
                    f"开仓价差=+{spread_data.spread_pct:.4f}% 平仓视角={-spread_data.spread_pct:.4f}%"
                )
                # 开仓详情在无持仓时最容易重复刷屏，增加节流保护
                self._log_with_throttle(
                    key=f"open_detail:{symbol}",
                    message=detail_msg,
                    throttle_seconds=10.0
                )
            
            # 🔥 判断是否为最后一笔拆单
            is_last_split = self.decision_engine.is_last_split_order(
                symbol=symbol,
                order_quantity=open_quantity,
                is_open=True
            )
            
            if (
                not self.executor.monitor_only
                and self._should_enforce_orderbook_liquidity(symbol)
            ):
                min_ob_qty = self._get_min_orderbook_quantity(symbol)
                legs = [
                    {
                        'exchange': spread_data.exchange_buy,
                        'symbol': buy_symbol,
                        'quantity': open_quantity,
                        'is_buy': True,
                        'desc': "开仓买入腿",
                        'min_quantity': min_ob_qty
                    },
                    {
                        'exchange': spread_data.exchange_sell,
                        'symbol': sell_symbol,
                        'quantity': open_quantity,
                        'is_buy': False,
                        'desc': "开仓卖出腿",
                        'min_quantity': min_ob_qty
                    },
                ]
                liquidity_ok, failure_detail = self._verify_orderbook_liquidity(
                    symbol,
                    legs,
                    action="开仓"
                )
                if not liquidity_ok:
                    self._log_liquidity_failure_summary(
                        symbol,
                        reason="V2开仓",
                        failure_detail=failure_detail,
                        base_message=f"⚠️ [V2开仓] {symbol}: 对手盘流动性不足，跳过本次拆单",
                        throttle_seconds=5.0,
                    )
                    return
                self._clear_liquidity_failure_summary("V2开仓", symbol)
            
            # 🔐 持续性确认日志（仅在实际下单前打印一次）
            self._log_persistence_confirmation(
                symbol=symbol,
                action="开仓",
                spread_pct=float(spread_data.spread_pct)
            )
            
            # 🔥 V2使用简化执行：直接执行开仓数量，不需要segment_id
            # 🔥 使用异步任务执行，避免阻塞主循环和UI更新
            open_key = self._build_open_pair_key(symbol, spread_data.exchange_buy, spread_data.exchange_sell)
            if not await self._try_register_open_pair(open_key):
                logger.debug(
                    "🔁 [V2开仓] %s %s→%s 已有执行任务，跳过重复触发",
                    symbol,
                    spread_data.exchange_buy,
                    spread_data.exchange_sell
                )
                return
            execution_task = asyncio.create_task(
                self._execute_open_with_lock(
                    symbol=symbol,
                    open_quantity=open_quantity,
                    spread_data=spread_data,
                    funding_rate_data=funding_rate_data,
                    is_last_split=is_last_split,
                    grid_level=grid_level,
                    slippage_pct=slippage_pct,
                    buy_limit_offset=buy_limit_offset,
                    sell_limit_offset=sell_limit_offset,
                    position=position,
                    open_key=open_key
                )
            )
            # 🔥 不等待执行完成，让任务在后台运行
            return
            
        except Exception as e:
            logger.error(f"❌ [统一调度] 开仓检查异常: {symbol}: {e}", exc_info=True)
    
    async def _execute_and_record_open(
        self,
        symbol: str,
        open_quantity: Decimal,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData],
        is_last_split: bool,
        grid_level: int,
        slippage_pct: Optional[Decimal],
        buy_limit_offset: Optional[Decimal],
        sell_limit_offset: Optional[Decimal],
        position: Optional[SegmentedPosition]
    ):
        """异步执行开仓并记录结果"""
        # 🔥 获取完整盘口数据（4组：买入腿Ask/Bid + 卖出腿Ask/Bid）
        buy_leg_symbol = spread_data.buy_symbol or symbol
        sell_leg_symbol = spread_data.sell_symbol or symbol
        orderbook_buy = self.data_processor.get_orderbook(spread_data.exchange_buy, buy_leg_symbol)
        orderbook_sell = self.data_processor.get_orderbook(spread_data.exchange_sell, sell_leg_symbol)
        
        orderbook_buy_ask = spread_data.price_buy if orderbook_buy and orderbook_buy.best_ask else None
        orderbook_buy_bid = orderbook_buy.best_bid.price if orderbook_buy and orderbook_buy.best_bid else None
        orderbook_sell_ask = orderbook_sell.best_ask.price if orderbook_sell and orderbook_sell.best_ask else None
        orderbook_sell_bid = spread_data.price_sell if orderbook_sell and orderbook_sell.best_bid else None

        grid_threshold_pct = self._resolve_grid_threshold_pct(symbol, grid_level)
        
        exec_request = ExecutionRequest(
            symbol=symbol,
            exchange_buy=spread_data.exchange_buy,
            exchange_sell=spread_data.exchange_sell,
            price_buy=spread_data.price_buy,
            price_sell=spread_data.price_sell,
            quantity=open_quantity,
            is_open=True,
            spread_data=spread_data,
            is_last_split=is_last_split,
            buy_symbol=buy_leg_symbol,
            sell_symbol=sell_leg_symbol,
            grid_action="open",
            grid_level=grid_level,
            grid_threshold_pct=grid_threshold_pct,
            slippage_tolerance_pct=slippage_pct,
            limit_price_offset_buy=buy_limit_offset,
            limit_price_offset_sell=sell_limit_offset,
            min_exchange_order_qty=self._build_min_exchange_order_qty_map(symbol),
            orderbook_buy_ask=orderbook_buy_ask,
            orderbook_buy_bid=orderbook_buy_bid,
            orderbook_sell_ask=orderbook_sell_ask,
            orderbook_sell_bid=orderbook_sell_bid,
        )
        result: Optional[ExecutionResult] = None
        try:
            result = await self.executor.execute_arbitrage(exec_request)

            if result.success:
                self._clear_dual_limit_backoff(symbol)
                entry_price_buy = self._resolve_execution_price(
                    result.order_buy,
                    spread_data.price_buy
                )
                entry_price_sell = self._resolve_execution_price(
                    result.order_sell,
                    spread_data.price_sell
                )

                filled_quantity = self._extract_filled_quantity(result, open_quantity)
                if filled_quantity + Decimal('0') < open_quantity:
                    logger.warning(
                        "⚠️ [V2开仓] %s: 实际成交 %s 低于目标 %s，剩余部分将延后补齐",
                        symbol,
                        filled_quantity,
                        open_quantity
                    )
                await self.decision_engine.record_open(
                    symbol=symbol,
                    quantity=open_quantity,
                    spread_data=spread_data,
                    funding_rate_data=funding_rate_data,
                    entry_price_buy=entry_price_buy,
                    entry_price_sell=entry_price_sell,
                    filled_quantity=filled_quantity
                )
                self.decision_engine.report_open_shortfall(
                    symbol=symbol,
                    requested_quantity=open_quantity,
                    actual_quantity=filled_quantity
                )

                actual_spread = entry_price_sell - entry_price_buy
                actual_spread_pct = (actual_spread / entry_price_buy) * 100
                profit_estimate = float(actual_spread * open_quantity)
                logger.info(
                    "✅ [开仓成交] %s | 数量=%s | "
                    "买%s@%.2f 卖%s@%.2f | "
                    "实际价差=%.4f%% 理论=%.4f%% | "
                    "预期盈利=$%.2f | "
                    "新持仓=%s",
                    symbol,
                    open_quantity,
                    spread_data.exchange_buy,
                    entry_price_buy,
                    spread_data.exchange_sell,
                    entry_price_sell,
                    actual_spread_pct,
                    spread_data.spread_pct,
                    profit_estimate,
                    position.total_quantity + open_quantity if position else open_quantity
                )
            else:
                if getattr(result, "failure_code", None) == "dual_limit_no_fill":
                    self._schedule_dual_limit_backoff(symbol)
                else:
                    self._clear_dual_limit_backoff(symbol)
        except Exception as e:
            logger.error(f"❌ [统一调度] 开仓执行异常: {symbol}: {e}", exc_info=True)
        finally:
            self._handle_emergency_close_feedback(symbol, exec_request, result, action_label="开仓")
            self._schedule_position_alignment(symbol)
    
    async def _execute_open_with_lock(
        self,
        *,
        symbol: str,
        open_quantity: Decimal,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData],
        is_last_split: bool,
        grid_level: int,
        slippage_pct: Optional[Decimal],
        buy_limit_offset: Optional[Decimal],
        sell_limit_offset: Optional[Decimal],
        position: Optional[SegmentedPosition],
        open_key: str
    ) -> None:
        try:
            await self._execute_and_record_open(
                symbol=symbol,
                open_quantity=open_quantity,
                spread_data=spread_data,
                funding_rate_data=funding_rate_data,
                is_last_split=is_last_split,
                grid_level=grid_level,
                slippage_pct=slippage_pct,
                buy_limit_offset=buy_limit_offset,
                sell_limit_offset=sell_limit_offset,
                position=position
            )
        finally:
            self._release_open_pair(open_key)
    
    def _extract_filled_quantity(
        self,
        execution_result: ExecutionResult,
        fallback: Decimal
    ) -> Decimal:
        """根据执行结果推断实际成交数量"""
        qty_decimal = self._safe_decimal(getattr(execution_result, "success_quantity", None))
        if qty_decimal > Decimal("0"):
            return qty_decimal

        derived = self._derive_quantity_from_orders(execution_result)
        if derived > Decimal("0"):
            return derived

        if fallback > Decimal("0"):
            logger.warning(
                "⚠️ [统一调度] 执行结果未返回实际成交量，已按0处理 (预期=%s)",
                fallback,
            )
        return Decimal("0")

    @staticmethod
    def _safe_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal("0")

    def _derive_quantity_from_orders(self, execution_result: ExecutionResult) -> Decimal:
        quantities: List[Decimal] = []
        for order in (execution_result.order_buy, execution_result.order_sell):
            if order is None:
                continue
            filled_value = getattr(order, "filled", None)
            filled_decimal = self._safe_decimal(filled_value)
            if filled_decimal > Decimal("0"):
                quantities.append(filled_decimal)
        if len(quantities) == 2:
            return min(quantities)
        if len(quantities) == 1:
            return quantities[0]
        return Decimal("0")
    
    async def _check_and_close(
        self,
        symbol: str,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData],
        *,
        config_symbol: Optional[str] = None
    ):
        """检查并执行平仓（V2总量驱动算法）"""
        try:
            # 🔥 检查是否处于人工介入等待状态
            if self.symbol_state_manager:
                # 先计算网格级别（需要在 should_block 检查前）
                grid_level = self.decision_engine.get_grid_level(symbol, spread_data.spread_pct)
                blocked, state = self.symbol_state_manager.should_block(symbol, grid_level)
                if blocked:
                    reason = state.reason if state else "等待中"
                    grid_level_display = state.grid_level if state else grid_level
                    self._log_with_throttle(
                        key=f"manual_intervention_close:{symbol}",
                        message=f"⏸️ [V2平仓] {symbol}: 当前处于等待状态，原因={reason}，保持跳过 (T{grid_level_display})",
                        throttle_seconds=60.0,
                    )
                    return
            
            if self.reduce_only_guard.is_pair_closing_blocked(symbol):
                self._log_with_throttle(
                    key=f"reduce_only_close_blocked:{symbol}",
                    message=f"⏸️ [V2平仓] {symbol}: 交易所仍处于 reduce-only 限制，等待恢复后再尝试平仓。",
                    throttle_seconds=60.0,
                )
                return
            if self.reduce_only_guard.is_pair_blocked(symbol):
                self._log_with_throttle(
                    key=f"reduce_only_close:{symbol}",
                    message=f"⏸️ [V2平仓] {symbol}: 交易所在 reduce-only 模式，暂停开平仓，等待整点探针恢复。",
                    throttle_seconds=60.0,
                )
                return
            symbol_config = self.config_manager.get_config(symbol)
            slippage_pct = self._resolve_slippage_pct(symbol, symbol_config)
            # 🔥 V2接口：返回(是否平仓, 平仓数量, 平仓原因, _)
            # segment_id现在总是返回None，因为我们使用总量驱动，不关心具体段
            should_close, close_quantity, reason, _ = await self.decision_engine.should_close(
                symbol,
                spread_data,
                funding_rate_data
            )
            
            if not should_close or close_quantity <= Decimal('0'):
                return

            if not self._passes_price_stability(symbol, spread_data, action="平仓"):
                return

            local_spread_threshold = getattr(
                symbol_config.grid_config,
                "max_local_orderbook_spread_pct",
                None,
            )
            local_spread_threshold_dec: Optional[Decimal] = None
            if local_spread_threshold is not None:
                try:
                    local_spread_threshold_dec = Decimal(str(local_spread_threshold))
                except (InvalidOperation, TypeError, ValueError):
                    logger.warning(
                        "⚠️ [配置] %s: max_local_orderbook_spread_pct=%s 无法解析，已忽略平仓第四门槛",
                        symbol,
                        local_spread_threshold,
                    )
                    local_spread_threshold_dec = None
            if (
                local_spread_threshold_dec is not None
                and local_spread_threshold_dec > Decimal("0")
            ):
                if not self._passes_local_orderbook_spread(
                    symbol=symbol,
                    spread_data=spread_data,
                    threshold_pct=local_spread_threshold_dec,
                ):
                    return
            
            # 🔥 获取持仓方向（用于正确计算平仓价差）
            position = self.decision_engine.get_position(symbol)
            if not position:
                logger.warning(f"⚠️  [V2平仓] {symbol}: 无法获取持仓信息，取消平仓")
                return
            
            # 平仓时需要反向交易：开仓时买入的交易所，平仓时卖出
            position_exchange_buy = position.exchange_buy    # 开仓时的买入方
            position_exchange_sell = position.exchange_sell  # 开仓时的卖出方
            buy_back_symbol = position.sell_symbol or symbol
            sell_leg_symbol = position.buy_symbol or symbol
            config_limit_offset = None
            if config_symbol:
                config_limit_offset = self._get_limit_price_offset_for_symbol(config_symbol)
            buy_limit_offset = config_limit_offset or self._get_limit_price_offset_for_symbol(buy_back_symbol)
            sell_limit_offset = config_limit_offset or self._get_limit_price_offset_for_symbol(sell_leg_symbol)
            
            position_segments = self.decision_engine.get_current_segments(symbol)
            
            # 🔥 平仓意图日志 - 单行格式,包含关键信息
            # 平仓视角：使用当前订单簿价格（spread_data 已经是从当前订单簿计算的）
            # spread_data 已经是平仓视角，直接使用其 exchange 和 price
            closing_view = (
                f"平仓视角: 买{spread_data.exchange_buy}@{spread_data.price_buy:.2f} "
                f"卖{spread_data.exchange_sell}@{spread_data.price_sell:.2f} "
                f"(价差={spread_data.spread_pct:.4f}%)"
            )
            
            # 🔥 开仓视角：从持仓记录中计算加权平均开仓价格
            # 因为可能分多次开仓，需要计算加权平均
            open_segments = position.get_open_segments()
            if open_segments:
                total_qty = sum(float(seg.open_quantity) for seg in open_segments)
                if total_qty > 0:
                    # 加权平均买入价格
                    opening_buy_price = Decimal(str(sum(
                        float(seg.open_price_buy) * float(seg.open_quantity)
                        for seg in open_segments
                    ) / total_qty))
                    
                    # 加权平均卖出价格
                    opening_sell_price = Decimal(str(sum(
                        float(seg.open_price_sell) * float(seg.open_quantity)
                        for seg in open_segments
                    ) / total_qty))
                else:
                    # 兜底：使用当前价格的反向
                    opening_buy_price = spread_data.price_sell
                    opening_sell_price = spread_data.price_buy
            else:
                # 兜底：使用当前价格的反向
                opening_buy_price = spread_data.price_sell
                opening_sell_price = spread_data.price_buy
            
            opening_spread_abs = opening_sell_price - opening_buy_price
            opening_spread_pct = float((opening_spread_abs / opening_buy_price) * 100) if opening_buy_price > 0 else 0.0
            
            opening_view = (
                f"开仓视角: 买{position_exchange_buy}@{opening_buy_price:.2f} "
                f"卖{position_exchange_sell}@{opening_sell_price:.2f} "
                f"(价差={opening_spread_pct:.4f}%)"
            )
            close_message = (
                f"📉 [平仓意图] {symbol} | "
                f"数量={close_quantity} | "
                f"剩余={position.total_quantity} | "
                f"{closing_view} | {opening_view} | 原因={reason}"
            )
            self._log_with_throttle(
                key=f"close_intent:{symbol}",
                message=close_message,
                throttle_seconds=3.0
            )

            # 🔥 平仓详细数据 - 仅在DEBUG模式或大额平仓时输出
            is_reverse_view = (
                (spread_data.exchange_buy or "").lower() == (position_exchange_sell or "").lower()
                and (spread_data.exchange_sell or "").lower() == (position_exchange_buy or "").lower()
            )

            if not is_reverse_view:
                rebuilt = None
                if getattr(self, "spread_pipeline", None):
                    rebuilt = self.spread_pipeline._build_spreads_from_position(symbol, position)
                if rebuilt:
                    _, closing_from_memory = rebuilt
                    if closing_from_memory:
                        logger.warning(
                            "⚠️ [V2平仓] %s: 接收到的价差方向与持仓不符，已按照记忆方向重新计算平仓视角。",
                            symbol,
                        )
                        spread_data = closing_from_memory
                        is_reverse_view = True
                    else:
                        logger.warning(
                            "⚠️ [V2平仓] %s: 记忆方向缺少盘口数据，无法修正平仓视角，暂停本次平仓。",
                            symbol,
                        )
                        return
                else:
                    logger.warning(
                        "⚠️ [V2平仓] %s: 平仓视角与持仓方向不符且无法回溯记忆盘口，暂停本次平仓。",
                        symbol,
                    )
                    return
            if is_reverse_view and (self.debug.is_debug_enabled() or close_quantity >= position.total_quantity * Decimal('0.5')):
                logger.info(
                    "🔁 [平仓详情] %s | "
                    "平仓视角: 买%s/%s@%.2f→卖%s/%s@%.2f (价差=%.4f%%) | "
                    "开仓视角: 价差=%.4f%%",
                    symbol,
                    spread_data.exchange_buy,
                    spread_data.buy_symbol or symbol,
                    float(spread_data.price_buy),
                    spread_data.exchange_sell,
                    spread_data.sell_symbol or symbol,
                    float(spread_data.price_sell),
                    spread_data.spread_pct,
                    -spread_data.spread_pct
                )
            
            # 🔥 判断是否为最后一笔拆单
            is_last_split = self.decision_engine.is_last_split_order(
                symbol=symbol,
                order_quantity=close_quantity,
                is_open=False
            )
            
            if (
                not self.executor.monitor_only
                and self._should_enforce_orderbook_liquidity(symbol)
            ):
                min_ob_qty = self._get_min_orderbook_quantity(symbol)
                # 🔥 使用 spread_data 的交易所和symbol（已经是平仓视角）
                legs = [
                    {
                        'exchange': spread_data.exchange_buy,
                        'symbol': spread_data.buy_symbol or symbol,
                        'quantity': close_quantity,
                        'is_buy': True,
                        'desc': "平仓买回腿",
                        'min_quantity': min_ob_qty
                    },
                    {
                        'exchange': spread_data.exchange_sell,
                        'symbol': spread_data.sell_symbol or symbol,
                        'quantity': close_quantity,
                        'is_buy': False,
                        'desc': "平仓卖出腿",
                        'min_quantity': min_ob_qty
                    },
                ]
                liquidity_ok, failure_detail = self._verify_orderbook_liquidity(
                    symbol,
                    legs,
                    action="平仓"
                )
                if not liquidity_ok:
                    self._log_liquidity_failure_summary(
                        symbol,
                        reason="V2平仓",
                        failure_detail=failure_detail,
                        base_message=f"⚠️ [V2平仓] {symbol}: 对手盘流动性不足，等待下次机会",
                        throttle_seconds=5.0,
                    )
                    return
                self._clear_liquidity_failure_summary("V2平仓", symbol)
            
            self._log_persistence_confirmation(
                symbol=symbol,
                action="平仓",
                spread_pct=float(spread_data.spread_pct)
            )
            
            # 🔥 V2平仓：直接使用 spread_data（已经是平仓视角）
            close_key = symbol.upper()
            if not await self._try_register_close_symbol(close_key):
                logger.debug("🔁 [V2平仓] %s 已有执行任务，跳过重复触发", symbol)
                return
            # spread_data 是从 build_closing_spread_from_orderbooks() 返回的
            execution_task = asyncio.create_task(
                self._execute_close_with_lock(
                    symbol=symbol,
                    close_quantity=close_quantity,
                    spread_data=spread_data,
                    reason=reason,
                    is_last_split=is_last_split,
                    position_segments=position_segments,
                    slippage_pct=slippage_pct,
                    buy_limit_offset=buy_limit_offset,
                    sell_limit_offset=sell_limit_offset,
                    position=position,
                    close_key=close_key,
                    funding_rate_data=funding_rate_data,
                )
            )
            # 🔥 不等待执行完成，让任务在后台运行
            return
            
        except Exception as e:
            logger.error(f"❌ [统一调度] 平仓检查异常: {symbol}: {e}", exc_info=True)
    
    async def _execute_and_record_close(
        self,
        symbol: str,
        close_quantity: Decimal,
        spread_data: SpreadData,
        reason: str,
        is_last_split: bool,
        position_segments: int,
        slippage_pct: Optional[Decimal],
        buy_limit_offset: Optional[Decimal],
        sell_limit_offset: Optional[Decimal],
        position: Optional[SegmentedPosition],
        funding_rate_data: Optional[FundingRateData],
    ):
        """异步执行平仓并记录结果"""
        # 🔥 关键修复：spread_data 已经是平仓视角的数据（由 build_closing_spread_from_orderbooks 
        # 或 calculate_multi_leg_closing_spread 计算），其 exchange/price/symbol 都是平仓时应该使用的，
        # 不需要再次反转！
        #
        # build_closing_spread_from_orderbooks() 的逻辑：
        # - 平仓 exchange_buy = 开仓 exchange_sell（已反转）
        # - 平仓 price_buy = 当前 exchange_sell 的 Ask（正确的平仓买入价）
        # - 平仓 exchange_sell = 开仓 exchange_buy（已反转）
        # - 平仓 price_sell = 当前 exchange_buy 的 Bid（正确的平仓卖出价）
        #
        # 如果再次反转，会导致价格匹配错误的交易所，无法成交！
        
        # 🔥 获取完整盘口数据（4组：买入腿Ask/Bid + 卖出腿Ask/Bid）
        buy_leg_symbol = spread_data.buy_symbol or symbol
        sell_leg_symbol = spread_data.sell_symbol or symbol
        orderbook_buy = self.data_processor.get_orderbook(spread_data.exchange_buy, buy_leg_symbol)
        orderbook_sell = self.data_processor.get_orderbook(spread_data.exchange_sell, sell_leg_symbol)
        
        orderbook_buy_ask = spread_data.price_buy if orderbook_buy and orderbook_buy.best_ask else None
        orderbook_buy_bid = orderbook_buy.best_bid.price if orderbook_buy and orderbook_buy.best_bid else None
        orderbook_sell_ask = orderbook_sell.best_ask.price if orderbook_sell and orderbook_sell.best_ask else None
        orderbook_sell_bid = spread_data.price_sell if orderbook_sell and orderbook_sell.best_bid else None

        grid_threshold_pct = self._resolve_grid_threshold_pct(symbol, position_segments)
        
        exec_request = ExecutionRequest(
            symbol=symbol,
            exchange_buy=spread_data.exchange_buy,      # ✅ 直接使用平仓视角的交易所
            exchange_sell=spread_data.exchange_sell,    # ✅ 直接使用平仓视角的交易所
            price_buy=spread_data.price_buy,            # ✅ 直接使用平仓视角的价格（Ask）
            price_sell=spread_data.price_sell,          # ✅ 直接使用平仓视角的价格（Bid）
            quantity=close_quantity,
            is_open=False,
            spread_data=spread_data,
            is_last_split=is_last_split,
            buy_symbol=buy_leg_symbol,                  # ✅ 平仓时买入的标的
            sell_symbol=sell_leg_symbol,                # ✅ 平仓时卖出的标的
            grid_action="close",
            grid_level=position_segments,
            grid_threshold_pct=grid_threshold_pct,
            slippage_tolerance_pct=slippage_pct,
            limit_price_offset_buy=buy_limit_offset,
            limit_price_offset_sell=sell_limit_offset,
            min_exchange_order_qty=self._build_min_exchange_order_qty_map(symbol),
            orderbook_buy_ask=orderbook_buy_ask,
            orderbook_buy_bid=orderbook_buy_bid,
            orderbook_sell_ask=orderbook_sell_ask,
            orderbook_sell_bid=orderbook_sell_bid,
        )
        result: Optional[ExecutionResult] = None
        try:
            result = await self.executor.execute_arbitrage(exec_request)
        
            if result.success:
                filled_qty = self._extract_filled_quantity(result, close_quantity)
                if filled_qty > Decimal("0"):
                    await self.decision_engine.record_close(
                        symbol=symbol,
                        quantity=filled_qty,
                        spread_data=spread_data,
                        reason=reason
                    )
                    # 🔥 record_close() 已经更新了 position.total_quantity，直接读取即可
                    remaining_qty = position.total_quantity if position else Decimal('0')
                    logger.info(
                        "✅ [平仓成交] %s | 数量=%s | 剩余=%s | 原因=%s",
                        symbol,
                        filled_qty,
                        remaining_qty,
                        reason
                    )
                    self._clear_dual_limit_backoff(symbol)
                else:
                    logger.warning(
                        "⚠️ [平仓成交] %s | 执行器报告成交量为0，决策引擎未更新",
                        symbol
                    )
                    self._clear_dual_limit_backoff(symbol)
            else:
                if getattr(result, "failure_code", None) == "dual_limit_no_fill":
                    self._schedule_dual_limit_backoff(symbol)
                else:
                    self._clear_dual_limit_backoff(symbol)
        except Exception as e:
            logger.error(f"❌ [统一调度] 平仓执行异常: {symbol}: {e}", exc_info=True)
        finally:
            self._handle_emergency_close_feedback(symbol, exec_request, result, action_label="平仓")
            self._schedule_position_alignment(symbol)
    
    async def _execute_close_with_lock(
        self,
        *,
        symbol: str,
        close_quantity: Decimal,
        spread_data: SpreadData,
        reason: str,
        is_last_split: bool,
        position_segments: List[PositionSegment],
        slippage_pct: Optional[Decimal],
        buy_limit_offset: Optional[Decimal],
        sell_limit_offset: Optional[Decimal],
        position: SegmentedPosition,
        close_key: str,
        funding_rate_data: Optional[FundingRateData],
    ) -> None:
        try:
            await self._execute_and_record_close(
                symbol=symbol,
                close_quantity=close_quantity,
                spread_data=spread_data,
                reason=reason,
                is_last_split=is_last_split,
                position_segments=position_segments,
                slippage_pct=slippage_pct,
                buy_limit_offset=buy_limit_offset,
                sell_limit_offset=sell_limit_offset,
                position=position,
                funding_rate_data=funding_rate_data,
            )
        finally:
            self._release_close_symbol(close_key)
    
    def _handle_emergency_close_feedback(
        self,
        symbol: str,
        request: ExecutionRequest,
        result: Optional[ExecutionResult],
        action_label: str,
    ) -> None:
        if not result or not getattr(result, "emergency_closes", None):
            return
        for entry in result.emergency_closes:
            qty_decimal = Decimal("0")
            try:
                qty_decimal = Decimal(str(entry.get("quantity", "0")))
            except Exception:
                qty_decimal = Decimal("0")
            logger.warning(
                "🧯 [紧急平仓反馈] %s %s | 交易所=%s | 数量=%s | 上下文=%s/%s | 状态=%s",
                symbol,
                action_label,
                entry.get("exchange", "-"),
                qty_decimal,
                entry.get("context", "-"),
                entry.get("exchange_role", "-"),
                entry.get("status", "-"),
            )

    def _schedule_position_alignment(self, symbol: str, delay_seconds: float = 1.0) -> None:
        """
        立即执行一次持仓校验，并在指定延迟后再次复查，
        以容忍 WebSocket 推送的短暂延迟。
        """
        try:
            self._audit_position_alignment(symbol)
        except Exception as exc:
            logger.debug("⚠️ [持仓校验] 即时校验异常(%s): %s", symbol, exc)

        async def _delayed_audit() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                self._audit_position_alignment(symbol)
            except Exception as delayed_exc:
                logger.debug("⚠️ [持仓校验] 延迟校验异常(%s): %s", symbol, delayed_exc)

        try:
            asyncio.create_task(_delayed_audit())
        except Exception as exc:
            logger.debug("⚠️ [持仓校验] 无法调度延迟任务(%s): %s", symbol, exc)

    def _audit_position_alignment(self, symbol: str) -> None:
        decision_map = self._collect_decision_net_positions()
        exchange_map = self._collect_exchange_net_positions()
        if not decision_map and not exchange_map:
            return
        audit_key = symbol or "__global__"
        consistent = self._position_maps_consistent(decision_map, exchange_map)
        decision_items = tuple(sorted(decision_map.items()))
        exchange_items = tuple(sorted(exchange_map.items()))
        snapshot = f"{decision_items}|{exchange_items}|{consistent}"
        last_snapshot = self._last_alignment_snapshot.get(audit_key)
        last_time = self._last_alignment_log_time.get(audit_key, 0.0)
        now = time.time()

        # 若状态一致且未变化且间隔未到，跳过重复打印
        if (
            consistent
            and snapshot == last_snapshot
            and now - last_time < self._alignment_log_interval
        ):
            return

        decision_display = self._format_position_map(decision_map)
        exchange_display = self._format_position_map(exchange_map)
        header = "✅ 一致" if consistent else "⚠️ 不一致"
        
        # 🔥 提取当前涉及的代币（用于显示上下文）
        symbol_tokens = set()
        if symbol:
            # 从 LIGHTER_PARADEX_SOL 提取 SOL
            parts = symbol.split('_')
            if len(parts) >= 3:
                symbol_tokens.add(parts[-1])  # 最后一个通常是代币名
        
        # 如果当前操作的代币有持仓，显示具体的交易对；否则显示全局
        has_relevant_position = any(
            token in symbol_tokens 
            for _, token in list(decision_map.keys()) + list(exchange_map.keys())
        )
        
        if has_relevant_position and symbol_tokens:
            title = f"🧮 持仓校验 - {symbol} | {header}"
        else:
            title = f"🧮 持仓校验 - 全局 (触发自: {symbol}) | {header}"
        
        base_lines = [
            "",
            "=" * 80,
            title,
            "=" * 80,
            "📊 决策引擎:",
            f"   {decision_display}",
            "",
            "📟 交易所缓存:",
            f"   {exchange_display}",
        ]
        mismatch_display = None
        if consistent:
            base_lines.append("=" * 80)
            logger.info("\n".join(base_lines))
        else:
            mismatch_display = self._format_position_deltas(decision_map, exchange_map)
            base_lines.extend(
                [
                    "",
                    "📋 差异明细:",
                    f"   {mismatch_display}" if mismatch_display else "   -",
                    "=" * 80,
                ]
            )
            logger.warning("\n".join(base_lines))

        # 供 UI 使用的简洁快照（不直接打印）
        self._last_alignment_ui_data = {
            "title": title,
            "consistent": consistent,
            "decision": decision_display,
            "exchange": exchange_display,
            "delta": mismatch_display,
            "timestamp": datetime.now().strftime("%m-%d %H:%M:%S"),
        }

        self._last_alignment_snapshot[audit_key] = snapshot
        self._last_alignment_log_time[audit_key] = now

    def get_alignment_ui_data(self) -> Optional[Dict[str, Any]]:
        """获取最近一次持仓校验的简洁快照，供UI显示。"""
        return self._last_alignment_ui_data

    def _collect_decision_net_positions(self) -> Dict[Tuple[str, str], Decimal]:
        """
        收集决策引擎的净持仓，按 (交易所, 标的) 分组
        
        对于多腿套利（例如 LIGHTER_PAXG_XAU），会拆分成：
        - ('lighter', 'XAU'): +0.043  (买入腿，标准化后)
        - ('lighter', 'PAXG'): -0.043 (卖出腿，标准化后)
        """
        totals: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
        epsilon = getattr(self.decision_engine, "quantity_epsilon", Decimal("0.0001"))

        pair_positions = getattr(self.decision_engine, "pair_positions", {}) or {}
        for pair_map in pair_positions.values():
            for pair_position in pair_map.values():
                qty = getattr(pair_position, "total_quantity", Decimal("0"))
                if qty <= epsilon:
                    continue
                
                buy_exchange = (pair_position.exchange_buy or "").lower()
                sell_exchange = (pair_position.exchange_sell or "").lower()
                buy_symbol_raw = (pair_position.buy_symbol or pair_position.symbol or "").upper()
                sell_symbol_raw = (pair_position.sell_symbol or pair_position.symbol or "").upper()
                
                # 🔥 标准化symbol名称，确保与交易所侧一致（去除 -USD-PERP 等后缀）
                buy_symbol = self._normalize_symbol_for_comparison(buy_symbol_raw)
                sell_symbol = self._normalize_symbol_for_comparison(sell_symbol_raw)
                
                if buy_exchange and buy_symbol:
                    totals[(buy_exchange, buy_symbol)] += qty
                if sell_exchange and sell_symbol:
                    totals[(sell_exchange, sell_symbol)] -= qty

        if not totals:
            raw_positions = getattr(self.decision_engine, "positions", {}) or {}
            for position in raw_positions.values():
                qty = getattr(position, "total_quantity", Decimal("0"))
                if qty <= epsilon:
                    continue
                
                buy_exchange = (position.exchange_buy or "").lower()
                sell_exchange = (position.exchange_sell or "").lower()
                buy_symbol_raw = (getattr(position, "buy_symbol", None) or position.symbol or "").upper()
                sell_symbol_raw = (getattr(position, "sell_symbol", None) or position.symbol or "").upper()
                
                # 🔥 标准化symbol名称，确保与交易所侧一致
                buy_symbol = self._normalize_symbol_for_comparison(buy_symbol_raw)
                sell_symbol = self._normalize_symbol_for_comparison(sell_symbol_raw)
                
                if buy_exchange and buy_symbol:
                    totals[(buy_exchange, buy_symbol)] += qty
                if sell_exchange and sell_symbol:
                    totals[(sell_exchange, sell_symbol)] -= qty

        return {
            key: qty for key, qty in totals.items() if abs(qty) > epsilon
        }

    def _collect_exchange_net_positions(self) -> Dict[Tuple[str, str], Decimal]:
        """
        收集交易所的实际持仓，按 (交易所, 标的) 分组
        
        对于多腿套利，会分别记录每个标的的持仓：
        - ('lighter', 'XAU-USD-PERP'): +0.043
        - ('lighter', 'PAXG-USD-PERP'): -0.0453
        """
        totals: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
        cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        try:
            cache = self._collect_exchange_position_cache()
            if cache:
                try:
                    self.ui_manager.update_exchange_position_cache(cache)
                except Exception as exc:
                    logger.debug("⚠️ [持仓校验] 无法刷新 UI 持仓缓存: %s", exc)
        except Exception as exc:
            logger.debug("⚠️ [持仓校验] 采集交易所持仓缓存失败: %s", exc)
            cache = {}

        if not cache:
            cache = getattr(self.ui_manager, "exchange_position_cache", None) or {}

        for exchange, positions in cache.items():
            exchange_key = (exchange or "").lower()
            if not exchange_key:
                continue
            for symbol, payload in positions.items():
                symbol_key_raw = (symbol or "").upper()
                symbol_key = self._normalize_symbol_for_comparison(symbol_key_raw)
                if not symbol_key:
                    continue
                    
                size_raw = payload.get("size", 0)
                try:
                    size = Decimal(str(size_raw))
                except Exception:
                    size = Decimal("0")
                side = str(payload.get("side") or "").strip().lower()
                if side.startswith("short") and size > Decimal("0"):
                    size = -size
                elif side.startswith("long") and size < Decimal("0"):
                    size = abs(size)
                totals[(exchange_key, symbol_key)] += size

        epsilon = getattr(self.decision_engine, "quantity_epsilon", Decimal("0.0001"))
        return {
            key: qty for key, qty in totals.items() if abs(qty) > epsilon
        }

    def _normalize_symbol_for_comparison(self, symbol: str) -> str:
        """
        标准化标的名称用于比较
        
        将 PAXG-USD-PERP 和 PAXG 都标准化为 PAXG，
        让它们在校验时能够匹配上
        """
        if not symbol:
            return ""
        # 统一分隔符，兼容 BTC/USDC:PERP、BTC-USDC:PERP 等形式
        base = symbol.upper().replace("/", "-").replace(":", "-")
        # 通过非字母数字切分，保留关键 Token，做到“包含即匹配”
        tokens = [tok for tok in re.split(r"[^A-Z0-9]+", base) if tok]
        if not tokens:
            return base
        suffix_whitelist = {"USD", "USDC", "USDT", "PERP", "SPOT", "FUTURES"}
        for token in tokens:
            if token not in suffix_whitelist:
                return token
        return tokens[0]

    def _position_maps_consistent(
        self,
        left: Dict[Tuple[str, str], Decimal],
        right: Dict[Tuple[str, str], Decimal],
    ) -> bool:
        """
        检查两个持仓映射是否一致（支持标的名称标准化匹配）
        
        例如：
        - left: ('lighter', 'PAXG-USD-PERP')
        - right: ('lighter', 'PAXG')
        会被标准化为同一个键进行比较
        """
        tolerance = getattr(self.decision_engine, "quantity_epsilon", Decimal("0.0001"))
        if tolerance <= Decimal("0"):
            tolerance = Decimal("0.0001")
        
        # 🔥 标准化所有键，让 PAXG 和 PAXG-USD-PERP 能够匹配
        left_normalized: Dict[Tuple[str, str], Decimal] = {}
        for (exchange, symbol), qty in left.items():
            normalized_key = (exchange, self._normalize_symbol_for_comparison(symbol))
            left_normalized[normalized_key] = left_normalized.get(normalized_key, Decimal("0")) + qty
        
        right_normalized: Dict[Tuple[str, str], Decimal] = {}
        for (exchange, symbol), qty in right.items():
            normalized_key = (exchange, self._normalize_symbol_for_comparison(symbol))
            right_normalized[normalized_key] = right_normalized.get(normalized_key, Decimal("0")) + qty
        
        # 使用标准化后的键进行比较
        keys = set(left_normalized.keys()) | set(right_normalized.keys())
        for key in keys:
            if abs(left_normalized.get(key, Decimal("0")) - right_normalized.get(key, Decimal("0"))) > tolerance:
                return False
        return True

    def _format_position_deltas(
        self,
        left: Dict[Tuple[str, str], Decimal],
        right: Dict[Tuple[str, str], Decimal],
    ) -> str:
        # 🔥 标准化所有键，确保 ETH 和 ETH-USDC-PERP 能够匹配
        left_normalized: Dict[Tuple[str, str], Decimal] = {}
        for (exchange, symbol), qty in left.items():
            normalized_key = (exchange, self._normalize_symbol_for_comparison(symbol))
            left_normalized[normalized_key] = left_normalized.get(normalized_key, Decimal("0")) + qty
        
        right_normalized: Dict[Tuple[str, str], Decimal] = {}
        for (exchange, symbol), qty in right.items():
            normalized_key = (exchange, self._normalize_symbol_for_comparison(symbol))
            right_normalized[normalized_key] = right_normalized.get(normalized_key, Decimal("0")) + qty
        
        # 使用标准化后的键进行格式化
        tolerance = getattr(self.decision_engine, "quantity_epsilon", Decimal("0.0001"))
        if tolerance <= Decimal("0"):
            tolerance = Decimal("0.0001")
        keys = sorted(set(left_normalized.keys()) | set(right_normalized.keys()))
        mismatch_parts: List[str] = []
        for key in keys:
            l_val = left_normalized.get(key, Decimal("0"))
            r_val = right_normalized.get(key, Decimal("0"))
            delta = r_val - l_val
            if abs(delta) <= tolerance:
                continue
            exchange, symbol = key
            mismatch_parts.append(
                f"{exchange}/{symbol}:决策={l_val:+.4f} 交易所={r_val:+.4f} 差={delta:+.4f}"
            )
        return " | ".join(mismatch_parts) if mismatch_parts else "-"

    def _format_position_map(self, data: Dict[Tuple[str, str], Decimal]) -> str:
        if not data:
            return "-"
        
        # 🔥 标准化所有键，确保 ETH 和 ETH-USDC-PERP 能够匹配
        normalized: Dict[Tuple[str, str], Decimal] = {}
        for (exchange, symbol), qty in data.items():
            normalized_key = (exchange, self._normalize_symbol_for_comparison(symbol))
            normalized[normalized_key] = normalized.get(normalized_key, Decimal("0")) + qty
        
        parts: List[str] = []
        for (exchange, symbol) in sorted(normalized.keys()):
            qty = normalized[(exchange, symbol)]
            parts.append(f"{exchange}/{symbol}:{qty:+.4f}")
        return " ".join(parts) if parts else "-"
    
    # ------------------------------------------------------------------ #
    # 组合执行锁
    # ------------------------------------------------------------------ #

    def _build_open_pair_key(
        self,
        symbol: str,
        exchange_buy: Optional[str],
        exchange_sell: Optional[str]
    ) -> str:
        symbol_key = (symbol or "").upper()
        buy_key = (exchange_buy or "").lower()
        sell_key = (exchange_sell or "").lower()
        return f"{symbol_key}:{buy_key}->{sell_key}"

    async def _try_register_open_pair(self, key: str) -> bool:
        async with self._pending_open_lock:
            if key in self._pending_open_pairs:
                return False
            self._pending_open_pairs.add(key)
            return True

    def _release_open_pair(self, key: str) -> None:
        if not key:
            return
        self._pending_open_pairs.discard(key)

    async def _try_register_close_symbol(self, key: str) -> bool:
        async with self._pending_close_lock:
            if key in self._pending_close_symbols:
                return False
            self._pending_close_symbols.add(key)
            return True

    def _release_close_symbol(self, key: str) -> None:
        if not key:
            return
        self._pending_close_symbols.discard(key)
    
    def _update_ui(self):
        """兼容旧接口，委托 UI 控制器执行。"""
        self.ui_controller.update_ui()
    
    def _get_min_order_size(self, symbol: str) -> Optional[Decimal]:
        try:
            config = self.config_manager.get_config(symbol)
            min_size = getattr(config.quantity_config, 'min_order_size', None)
            if min_size in (None, 0):
                return None
            return Decimal(str(min_size))
        except Exception:
            return None

    def _build_min_exchange_order_qty_map(self, symbol: str) -> Dict[str, Decimal]:
        """
        根据 symbol 配置构建 {exchange: min_qty} 映射，供执行器约束最小下单量。
        """
        result: Dict[str, Decimal] = {}
        try:
            config = self.config_manager.get_config(symbol)
            qty_cfg = getattr(config, "quantity_config", None)
            raw_map = getattr(qty_cfg, "exchange_min_order_qty", None) or {}
            if isinstance(raw_map, dict):
                for exch, value in raw_map.items():
                    if value in (None, 0):
                        continue
                    try:
                        result[str(exch).lower()] = Decimal(str(value))
                    except (InvalidOperation, ValueError, TypeError):
                        logger.warning(
                            "⚠️ [配置] %s exchange_min_order_qty[%s]=%s 无法解析，已忽略",
                            symbol,
                            exch,
                            value,
                        )
        except Exception as exc:
            logger.warning("⚠️ [配置] 读取 %s 最小下单量失败: %s", symbol, exc)
        return result

    async def _ui_update_loop(self):
        await self.ui_controller._ui_update_loop()
    
    async def _update_ui_comprehensive(self):
        """兼容旧接口，委托 UI 控制器执行。"""
        await self.ui_controller.update_comprehensive()
    
    def _collect_multi_leg_data(self) -> List[Dict[str, Any]]:
        return self.ui_controller._collect_multi_leg_data()
    
    def _collect_grid_ui_data(self) -> Dict[str, Any]:
        return self.ui_controller._collect_grid_ui_data()
    
    def _collect_exchange_position_cache(self) -> Dict[str, Dict[str, Dict]]:
        return self.ui_controller._collect_exchange_position_cache()
    
    def _build_local_position_rows(self) -> List[Dict]:
        return self.ui_controller._build_local_position_rows()
    
    def _build_position_rows(self) -> List[Dict]:
        return self.ui_controller._build_position_rows()
    
    async def _update_account_balances_ui(self):
        await self.ui_controller._update_account_balances_ui()
    
    def _collect_ui_market_data(
        self
    ) -> Tuple[
        Dict[str, Dict[str, OrderBookData]],
        Dict[str, Dict[str, object]],
        Dict[str, List]
    ]:
        return self.ui_controller._collect_ui_market_data()

    def _capture_decision_snapshot(self, symbol: str, spread_data: SpreadData) -> None:
        """记录一次决策引擎状态，供心跳日志使用"""
        position = self.decision_engine.positions.get(symbol)
        quantity_epsilon = getattr(self.decision_engine, "quantity_epsilon", Decimal("0"))
        open_segments = position.get_segment_count() if position else 0
        completed_segments = (
            position.get_completed_segment_count(quantity_epsilon) if position else 0
        )
        active_segment = (
            position.get_active_incomplete_segment(quantity_epsilon) if position else None
        )
        if active_segment:
            next_segment_id = active_segment.segment_id
        elif position:
            next_segment_id = position.get_next_segment_id()
        else:
            next_segment_id = 1
        
        config = self.config_manager.get_config(symbol)
        grid_cfg = config.grid_config
        segment_index = max(0, next_segment_id - 1)
        next_threshold = grid_cfg.initial_spread_threshold + segment_index * grid_cfg.grid_step
        
        net_spread_pct = spread_data.spread_pct
        calc_net = getattr(self.decision_engine, "_calculate_net_spread", None)
        if callable(calc_net):
            try:
                net_spread_pct = calc_net(spread_data)
            except Exception:
                pass
        
        self._decision_snapshots[symbol] = {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
            "spread_pct": spread_data.spread_pct,
            "net_spread_pct": net_spread_pct,
            "next_threshold_pct": next_threshold,
            "next_segment_id": next_segment_id,
            "open_segments": open_segments,
            "completed_segments": completed_segments,
            "max_segments": grid_cfg.max_segments,
        }

    def _log_status_summary(self) -> None:
        """
        周期性输出状态汇总,让用户快速了解系统当前状态
        包括:持仓情况、价差状态、流动性状态、风险控制状态
        """
        current_time = time.time()
        if current_time - self._last_status_summary_time < self.status_summary_interval:
            return
        
        self._last_status_summary_time = current_time
        
        # 构建符号池：单交易对 + 多腿 + 多交易所 + 决策引擎已有持仓
        symbol_pool: Set[str] = set(self.monitor_config.symbols)
        if self.multi_leg_pairs:
            symbol_pool.update(pair.pair_id for pair in self.multi_leg_pairs)
        if self.multi_exchange_pairs:
            symbol_pool.update(pair.trading_pair_id for pair in self.multi_exchange_pairs)
        symbol_pool.update(self.decision_engine.positions.keys())
        
        # 收集持仓信息
        positions_summary = []
        for symbol in sorted(symbol_pool):
            position = self.decision_engine.get_position(symbol)
            if position and position.total_quantity > self.decision_engine.quantity_epsilon:
                positions_summary.append(
                    f"{symbol}={position.total_quantity.normalize() if hasattr(position.total_quantity, 'normalize') else position.total_quantity}"
                )
        
        # 收集价差信息
        spread_summary = []
        for symbol in sorted(symbol_pool):
            spreads = self.symbol_spreads.get(symbol, [])
            if spreads:
                best_spread = max(spreads, key=lambda s: s.spread_pct)
                spread_summary.append(
                    f"{symbol}={best_spread.spread_pct:.4f}%"
                )
        
        # 收集reduce-only状态
        blocked_pairs = []
        if self.reduce_only_guard:
            for symbol in sorted(symbol_pool):
                if self.reduce_only_guard.is_pair_blocked(symbol):
                    blocked_pairs.append(symbol)
        
        # 构建汇总信息
        status_parts = []
        
        if positions_summary:
            status_parts.append(f"持仓: {', '.join(positions_summary)}")
        else:
            status_parts.append("持仓: 无")
        
        if spread_summary:
            status_parts.append(f"价差: {', '.join(spread_summary)}")
        
        if blocked_pairs:
            status_parts.append(f"受限: {', '.join(blocked_pairs)}")
        
        # 输出汇总日志
        logger.info("📊 [状态汇总] %s", " | ".join(status_parts))
    
    def _log_decision_heartbeat(self) -> None:
        """按照固定间隔输出分段决策摘要，避免长时间无日志"""
        current_time = time.time()
        if current_time - self._last_decision_log_time < self.decision_log_interval:
            return
        
        self._last_decision_log_time = current_time
        
        if not self._decision_snapshots:
            logger.info("🫀 [分段决策] 心跳：暂无有效价差数据，等待订单簿更新")
            return
        
        lines = []
        for symbol in sorted(self._decision_snapshots.keys()):
            snap = self._decision_snapshots[symbol]
            lines.append(
                "  - {symbol}: spread={spread:.4f}% | net={net:.4f}% | "
                "next=第{next_id}段≥{threshold:.4f}% | segments {open_cnt}/{max_cnt}".format(
                    symbol=symbol,
                    spread=snap["spread_pct"],
                    net=snap["net_spread_pct"],
                    next_id=snap["next_segment_id"],
                    threshold=snap["next_threshold_pct"],
                    open_cnt=snap["open_segments"],
                    max_cnt=snap["max_segments"],
                )
            )
        
        logger.info("🫀 [分段决策] 心跳概览\n" + "\n".join(lines))
    
    def _get_quantity_config(self, symbol: str):
        """获取交易对的数量配置"""
        from ..config.arbitrage_config import QuantityConfig
        symbol_upper = symbol.upper()
        config = self.config_manager.get_config(symbol_upper).quantity_config
        if not config:
            config = QuantityConfig()
        return config
    
    def _calculate_segment_target_quantity(
        self,
        symbol: str,
        quantity_config
    ) -> Decimal:
        """计算单段应持有的目标数量"""
        base_quantity = Decimal(str(quantity_config.base_quantity or 0))
        ratio = Decimal('1')
        target_quantity = base_quantity * ratio
        return self._format_quantity(symbol, target_quantity)
    
    def _calculate_partial_order_quantity(
        self,
        symbol: str,
        target_quantity: Decimal,
        remaining_quantity: Decimal
    ) -> Decimal:
        """根据拆单配置计算本次应下单的数量"""
        if remaining_quantity <= Decimal('0'):
            return Decimal('0')
        
        grid_cfg = self.config_manager.get_config(symbol).grid_config
        ratio_value = grid_cfg.segment_partial_order_ratio or 1.0
        ratio = Decimal(str(ratio_value))
        if ratio <= Decimal('0'):
            chunk = remaining_quantity
        else:
            ratio = min(ratio, Decimal('1'))
            chunk = target_quantity * ratio
        
        if chunk <= Decimal('0'):
            chunk = remaining_quantity
        
        order_quantity = min(remaining_quantity, chunk)
        
        min_partial = Decimal(str(grid_cfg.min_partial_order_quantity or 0))
        if min_partial > Decimal('0') and order_quantity < min_partial:
            if remaining_quantity < min_partial:
                order_quantity = remaining_quantity
            else:
                order_quantity = min(remaining_quantity, max(chunk, min_partial))
        
        order_quantity = self._format_quantity(symbol, order_quantity)
        if order_quantity <= Decimal('0') and remaining_quantity > Decimal('0'):
            order_quantity = self._format_quantity(symbol, remaining_quantity)
        return order_quantity
    
    def _format_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        """按交易对精度格式化数量"""
        from decimal import ROUND_DOWN, InvalidOperation
        config = self._get_quantity_config(symbol)
        precision = max(0, getattr(config, 'quantity_precision', 4) or 0)
        step = Decimal('1').scaleb(-precision)
        try:
            formatted = quantity.quantize(step, rounding=ROUND_DOWN)
        except InvalidOperation:
            formatted = quantity
        return formatted
    
    def _resolve_execution_price(
        self,
        order,
        fallback: Decimal
    ) -> Decimal:
        """
        获取订单的实际成交价，若不存在则回退到理论价格
        """
        from typing import Any, Optional
        
        def _safe_decimal(value: Any) -> Optional[Decimal]:
            """安全地将任意值转换为 Decimal"""
            if value is None:
                return None
            if isinstance(value, Decimal):
                return value
            try:
                return Decimal(str(value))
            except:
                return None
        
        price_candidates: List[Optional[Decimal]] = []
        
        if order:
            price_candidates.extend([
                order.average,
                order.price,
            ])
            
            # 兼容 trades / raw_data 中的价格
            for trade in order.trades or []:
                price_candidates.append(_safe_decimal(trade.get("price")))
            
            if order.raw_data:
                price_candidates.append(_safe_decimal(order.raw_data.get("price")))
        
        for candidate in price_candidates:
            if candidate is not None and candidate > Decimal("0"):
                return candidate
        
        return fallback


# 主函数
async def main():
    """主函数"""
    orchestrator = UnifiedOrchestrator()
    
    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("⚠️  [统一调度] 收到中断信号")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())

