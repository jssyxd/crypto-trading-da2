"""
统一配置管理器

职责：
- 加载和管理套利系统的所有配置（决策、执行、风险控制）
- 提供配置访问接口
- 支持配置验证
- 支持配置热更新（可选）

注意：此模块仅用于套利监控系统v2，不影响其他系统
"""

import yaml
import logging
import asyncio
from typing import Dict, Any, Optional, Callable
from pathlib import Path
from dataclasses import asdict
from datetime import datetime

from .arbitrage_config import (
    ArbitrageUnifiedConfig,
    SystemModeConfig,
    DecisionConfig,
    DecisionThresholdsConfig,
    ExecutionConfig,
    QuantityConfig,
    ExchangeOrderModeConfig,
    ExchangeFeeConfig,
    ExchangeRateLimitConfig,
    OrderExecutionConfig,
    ExecutionRiskControlConfig,
    RiskControlConfig,
    PositionManagementConfig,
    BalanceManagementConfig,
    NetworkFailureConfig,
    ExchangeMaintenanceConfig,
    PriceAnomalyConfig,
    OrderAnomalyConfig,
    FundingRateAnomalyConfig,
    PositionDurationConfig,
    SingleTradeRiskConfig,
    DailyTradeLimitConfig,
    SymbolRiskConfig,
    DataConsistencyConfig,
)

# 🔥 使用统一日志系统
from core.adapters.exchanges.utils.setup_logging import LoggingConfig

logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file='arbitrage_config.log',
    console_formatter=None,
    file_formatter='detailed',
    level=logging.INFO
)


class UnifiedConfigManager:
    """统一配置管理器"""
    
    def __init__(
        self,
        config_path: Optional[Path] = None,
        enable_hot_reload: bool = False,
        hot_reload_interval: int = 5
    ):
        """
        初始化统一配置管理器
        
        Args:
            config_path: 配置文件路径，如果为None则使用默认配置
            enable_hot_reload: 是否启用热更新（默认False）
            hot_reload_interval: 热更新检查间隔（秒，默认5秒）
        """
        self.config_path = config_path
        self.config = ArbitrageUnifiedConfig()
        self.enable_hot_reload = enable_hot_reload
        self.hot_reload_interval = hot_reload_interval
        
        # 文件修改时间跟踪
        self._last_modified_time: Optional[float] = None
        
        # 配置变更回调
        self._on_config_changed: Optional[Callable[[ArbitrageUnifiedConfig], None]] = None
        
        # 热更新任务
        self._hot_reload_task: Optional[asyncio.Task] = None
        self._running = False
        
        if config_path and config_path.exists():
            self._load_from_file()
            if self.enable_hot_reload:
                self._last_modified_time = config_path.stat().st_mtime
        else:
            self._load_defaults()
    
    def _load_from_file(self):
        """从YAML文件加载配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            
            # 加载系统运行模式配置
            if 'system_mode' in data:
                self._load_system_mode_config(data['system_mode'])
            
            # 加载套利决策配置
            if 'arbitrage_decision' in data:
                self._load_decision_config(data['arbitrage_decision'])
            
            # 加载套利执行配置
            if 'arbitrage_execution' in data:
                self._load_execution_config(data['arbitrage_execution'])
            
            # 加载风险控制配置
            if 'risk_control' in data:
                self._load_risk_control_config(data['risk_control'])
            
            logger.info(f"✅ [配置管理] 配置已从文件加载: {self.config_path}")
            logger.info(f"📊 [配置管理] 系统模式: {'🔍 监控模式' if self.config.system_mode.monitor_only else '⚡ 实盘模式'}")
            
        except Exception as e:
            logger.warning(f"⚠️  [配置管理] 加载配置文件失败: {e}，使用默认配置", exc_info=True)
            self._load_defaults()
    
    def _load_system_mode_config(self, data: Dict[str, Any]):
        """加载系统运行模式配置"""
        if 'monitor_only' in data:
            self.config.system_mode.monitor_only = data['monitor_only']
            logger.info(f"🎯 [配置管理] 监控模式: {data['monitor_only']}")
        
        # 🔥 加载数据新鲜度配置
        if 'data_freshness_seconds' in data:
            self.config.system_mode.data_freshness_seconds = float(data['data_freshness_seconds'])
            logger.info(f"📊 [配置管理] 数据新鲜度阈值: {self.config.system_mode.data_freshness_seconds}秒")
    
    def _load_decision_config(self, data: Dict[str, Any]):
        """加载套利决策配置"""
        if 'thresholds' in data:
            thresholds = data['thresholds']
            self.config.decision.thresholds = DecisionThresholdsConfig(
                spread_arbitrage_threshold=thresholds.get('spread_arbitrage_threshold', thresholds.get('X', 0.1)),
                    spread_persistence_seconds=int(thresholds.get('spread_persistence_seconds', 1)),
                funding_rate_diff_threshold=thresholds.get('funding_rate_diff_threshold', thresholds.get('Y', 0.01)),
                ignorable_spread_threshold=thresholds.get('ignorable_spread_threshold', thresholds.get('A', 0.05)),
                ignorable_funding_rate_diff_threshold=thresholds.get('ignorable_funding_rate_diff_threshold', thresholds.get('B', 0.01)),
                history_hours=thresholds.get('history_hours', thresholds.get('N', 24)),
                unfavorable_funding_rate_duration_minutes=thresholds.get('unfavorable_funding_rate_duration_minutes', thresholds.get('M', 60)),
                profit_target_threshold=thresholds.get('profit_target_threshold', thresholds.get('P', 0.5)),
                loss_stop_threshold=thresholds.get('loss_stop_threshold', thresholds.get('Q', 0.3)),
                funding_rate_annual_threshold=thresholds.get('funding_rate_annual_threshold', thresholds.get('F', 10.0)),
                funding_rate_stability_threshold=thresholds.get('funding_rate_stability_threshold', thresholds.get('S_FR', 0.5)),
                spread_stability_threshold=thresholds.get('spread_stability_threshold', thresholds.get('S_SP', 0.5)),
            )
        
        if 'enabled' in data:
            self.config.decision.enabled = data['enabled']
        if 'enable_spread_arbitrage' in data:
            self.config.decision.enable_spread_arbitrage = data['enable_spread_arbitrage']
        if 'enable_funding_rate_arbitrage' in data:
            self.config.decision.enable_funding_rate_arbitrage = data['enable_funding_rate_arbitrage']
    
    def _load_execution_config(self, data: Dict[str, Any]):
        """加载套利执行配置"""
        if 'default_order_mode' in data:
            self.config.execution.default_order_mode = data['default_order_mode']
        
        # 🔥 加载数量配置
        if 'quantity_config' in data:
            for symbol_or_default, qty_data in data['quantity_config'].items():
                precision_value = qty_data.get('quantity_precision')
                if precision_value is None:
                    precision_value = 4
                
                single_qty = qty_data.get('single_order_quantity')
                if single_qty is None and 'single_order_amount_usdc' in qty_data:
                    single_qty = qty_data.get('single_order_amount_usdc')
                    logger.warning(
                        f"⚠️ [配置管理] {symbol_or_default}: single_order_amount_usdc 已弃用，"
                        "其值将直接作为数量使用，请尽快改为 single_order_quantity。"
                    )
                if single_qty is None:
                    logger.warning(
                        f"⚠️ [配置管理] {symbol_or_default}: 未配置 single_order_quantity，使用默认0.1"
                    )
                    single_qty = 0.1
                
                max_qty = qty_data.get('max_position_quantity')
                if max_qty is None and 'max_position_usdc' in qty_data:
                    max_qty = qty_data.get('max_position_usdc')
                    logger.warning(
                        f"⚠️ [配置管理] {symbol_or_default}: max_position_usdc 已弃用，"
                        "其值将直接作为数量使用，请尽快改为 max_position_quantity。"
                    )
                if max_qty is None:
                    logger.warning(
                        f"⚠️ [配置管理] {symbol_or_default}: 未配置 max_position_quantity，使用默认0.2"
                    )
                    max_qty = 0.2
                
                self.config.execution.quantity_config[symbol_or_default] = QuantityConfig(
                    single_order_quantity=float(single_qty),
                    max_position_quantity=float(max_qty),
                    quantity_precision=int(precision_value),
                )
            logger.info(f"✅ [配置管理] 数量配置已加载: {len(self.config.execution.quantity_config)}个代币")
        
        # 加载交易所下单模式配置
        if 'exchange_order_modes' in data:
            for exchange_name, mode_data in data['exchange_order_modes'].items():
                raw_order_mode = mode_data.get('order_mode')
                order_mode = (
                    str(raw_order_mode).strip().lower()
                    if isinstance(raw_order_mode, str)
                    else None
                )
                if order_mode not in ('limit', 'market'):
                    order_mode = None
                
                buy_mode_legacy = mode_data.get('buy_mode')
                sell_mode_legacy = mode_data.get('sell_mode')
                if order_mode is None:
                    candidate = buy_mode_legacy or sell_mode_legacy
                    if isinstance(candidate, str) and candidate.lower() in ('limit', 'market'):
                        order_mode = candidate.lower()
                    else:
                        order_mode = 'market'
                
                priority_value = mode_data.get('priority', 0)
                try:
                    priority = int(priority_value)
                except (TypeError, ValueError):
                    logger.warning(
                        f"⚠️ [配置管理] {exchange_name} priority 配置无效({priority_value})，使用0"
                    )
                    priority = 0
                
                limit_price_offset_source = None
                if 'limit_price_offset_pct' in mode_data:
                    limit_price_offset_source = 'limit_price_offset_pct'
                    raw_limit_price_offset = mode_data.get('limit_price_offset_pct')
                else:
                    limit_price_offset_source = 'limit_price_offset'
                    raw_limit_price_offset = mode_data.get('limit_price_offset', 0.001)
                try:
                    limit_price_offset = float(raw_limit_price_offset)
                except (TypeError, ValueError):
                    logger.warning(
                        f"⚠️ [配置管理] {exchange_name} {limit_price_offset_source} 配置无效({raw_limit_price_offset})，使用0.001"
                    )
                    limit_price_offset = 0.001
                else:
                    if limit_price_offset_source == 'limit_price_offset':
                        logger.debug(
                            "ℹ️ [配置管理] %s 仍在使用 limit_price_offset，将于后续版本更名为 limit_price_offset_pct",
                            exchange_name,
                        )
                
                exchange_config = ExchangeOrderModeConfig(
                    order_mode=order_mode,
                    priority=priority,
                    limit_price_offset=limit_price_offset,
                    buy_mode=buy_mode_legacy,
                    sell_mode=sell_mode_legacy,
                )
                
                if 'use_tick_precision' in mode_data:
                    setattr(
                        exchange_config,
                        'use_tick_precision',
                        bool(mode_data.get('use_tick_precision'))
                    )
                
                # 🔥 处理 force_sequential_limit 参数
                if 'force_sequential_limit' in mode_data:
                    force_seq = bool(mode_data.get('force_sequential_limit'))
                    setattr(
                        exchange_config,
                        'force_sequential_limit',
                        force_seq
                    )
                    if force_seq:
                        logger.info(
                            f"⚙️ [配置管理] {exchange_name}: 已启用 force_sequential_limit (强制顺序执行)"
                        )
                
                if 'force_limit_when_second_leg' in mode_data:
                    force_limit_second = bool(mode_data.get('force_limit_when_second_leg'))
                    setattr(
                        exchange_config,
                        'force_limit_when_second_leg',
                        force_limit_second
                    )
                    if force_limit_second:
                        logger.info(
                            f"⚙️ [配置管理] {exchange_name}: 限价+市价模式中作为第二腿时将改用激进限价执行"
                        )
                
                if 'reuse_signal_price_when_second_leg' in mode_data:
                    reuse_signal_price_second = bool(mode_data.get('reuse_signal_price_when_second_leg'))
                    setattr(
                        exchange_config,
                        'reuse_signal_price_when_second_leg',
                        reuse_signal_price_second
                    )
                    if reuse_signal_price_second:
                        logger.info(
                            f"⚙️ [配置管理] {exchange_name}: 第二腿激进限价将复用决策引擎的信号价格"
                        )
                
                if 'smart_second_leg_price' in mode_data:
                    smart_second = bool(mode_data.get('smart_second_leg_price'))
                    setattr(
                        exchange_config,
                        'smart_second_leg_price',
                        smart_second
                    )
                    if smart_second:
                        logger.info(
                            f"⚙️ [配置管理] {exchange_name}: 已启用 smart_second_leg_price (智能盘口价格切换)"
                        )
                
                self.config.execution.exchange_order_modes[exchange_name] = exchange_config
            logger.info(
                "✅ [配置管理] 交易所下单模式已加载: "
                f"{len(self.config.execution.exchange_order_modes)} 个"
            )
        
        # 加载交易所手续费配置
        if 'exchange_fees' in data:
            for exchange_name, fee_data in data['exchange_fees'].items():
                limit_fee_rate = fee_data.get('limit_fee_rate', 0.0001)
                market_fee_rate = fee_data.get('market_fee_rate', 0.0003)
                try:
                    limit_fee_rate = float(limit_fee_rate)
                except (TypeError, ValueError):
                    logger.warning(
                        f"⚠️ [配置管理] {exchange_name} limit_fee_rate 无效({limit_fee_rate})，使用0.0001"
                    )
                    limit_fee_rate = 0.0001
                try:
                    market_fee_rate = float(market_fee_rate)
                except (TypeError, ValueError):
                    logger.warning(
                        f"⚠️ [配置管理] {exchange_name} market_fee_rate 无效({market_fee_rate})，使用0.0003"
                    )
                    market_fee_rate = 0.0003
                
                self.config.execution.exchange_fee_config[exchange_name] = ExchangeFeeConfig(
                    limit_fee_rate=limit_fee_rate,
                    market_fee_rate=market_fee_rate,
                )
            logger.info(
                "✅ [配置管理] 交易所手续费配置已加载: "
                f"{len(self.config.execution.exchange_fee_config)} 个"
            )
        
        if 'default' not in self.config.execution.exchange_fee_config:
            self.config.execution.exchange_fee_config['default'] = ExchangeFeeConfig()

        # 加载交易所限速配置
        if 'exchange_rate_limits' in data:
            limits_data = data['exchange_rate_limits']
            loaded_count = 0
            for exchange_name, limit_data in limits_data.items():
                try:
                    cfg = ExchangeRateLimitConfig(
                        max_concurrent_orders=int(limit_data.get('max_concurrent_orders', 1) or 1),
                        min_interval_ms=int(limit_data.get('min_interval_ms', 0) or 0),
                        rate_limit_cooldown_ms=int(limit_data.get('rate_limit_cooldown_ms', 1000) or 0),
                    )
                except Exception as parse_error:
                    logger.warning(
                        "⚠️ [配置管理] %s 限速配置无效(%s)，使用默认值。",
                        exchange_name,
                        parse_error
                    )
                    cfg = ExchangeRateLimitConfig()
                self.config.execution.exchange_rate_limits[exchange_name] = cfg
                loaded_count += 1
            if loaded_count:
                logger.info("✅ [配置管理] 交易所限速配置已加载: %d 条", loaded_count)
        
        # 加载订单执行配置
        if 'order_execution' in data:
            exec_data = data['order_execution']
            lighter_timeout_raw = exec_data.get('lighter_market_order_timeout')
            lighter_timeout: Optional[int] = None
            if lighter_timeout_raw not in (None, ""):
                try:
                    lighter_timeout = int(lighter_timeout_raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "⚠️ [配置管理] lighter_market_order_timeout 无效(%s)，已忽略",
                        lighter_timeout_raw,
                    )
                    lighter_timeout = None
            self.config.execution.order_execution = OrderExecutionConfig(
                limit_order_timeout=exec_data.get('limit_order_timeout', 60),
                lighter_market_order_timeout=lighter_timeout,
                max_retry_count=exec_data.get('max_retry_count', 3),
                partial_fill_strategy=exec_data.get('partial_fill_strategy', 'continue'),
                round_pause_seconds=exec_data.get('round_pause_seconds', 60),
                enable_dual_limit_mode=exec_data.get('enable_dual_limit_mode', False),
                dual_limit_retry_initial_delay=exec_data.get('dual_limit_retry_initial_delay', 30),
                dual_limit_retry_max_delay=exec_data.get('dual_limit_retry_max_delay', 600),
                dual_limit_retry_backoff_factor=exec_data.get('dual_limit_retry_backoff_factor', 2.0),
                slippage_protection_enabled=exec_data.get('slippage_protection', {}).get('enabled', True),
                max_slippage=exec_data.get('slippage_protection', {}).get('max_slippage', 0.005),
            )
        
        # 兼容 segmented.yaml 中 legacy execution.order_timeout_seconds
        legacy_execution = data.get('execution', {})
        legacy_timeout = legacy_execution.get('order_timeout_seconds')
        if legacy_timeout is not None:
            try:
                timeout_val = int(legacy_timeout)
                if timeout_val > 0:
                    current_timeout = getattr(
                        self.config.execution.order_execution,
                        'limit_order_timeout',
                        None,
                    )
                    if not current_timeout or current_timeout == 60:
                        self.config.execution.order_execution.limit_order_timeout = timeout_val
                        logger.info(
                            "🔄 [配置管理] 使用 legacy execution.order_timeout_seconds=%s "
                            "覆盖 limit_order_timeout",
                            timeout_val,
                        )
            except (TypeError, ValueError):
                logger.warning(
                    "⚠️ [配置管理] legacy execution.order_timeout_seconds 无效(%s)",
                    legacy_timeout,
                )
        
        # 加载执行风险控制配置
        if 'risk_control' in data:
            risk_data = data['risk_control']
            self.config.execution.risk_control = ExecutionRiskControlConfig(
                max_order_size=risk_data.get('max_order_size', 1.0),
                min_order_size=risk_data.get('min_order_size', 0.001),
                price_change_check_enabled=risk_data.get('price_change_check', {}).get('enabled', True),
                max_price_change=risk_data.get('price_change_check', {}).get('max_price_change', 0.01),
            )
    
    def _load_risk_control_config(self, data: Dict[str, Any]):
        """加载风险控制配置"""
        # 仓位管理
        if 'position_management' in data:
            pm_data = data['position_management']
            self.config.risk_control.position_management = PositionManagementConfig(
                max_single_token_position=pm_data.get('max_single_token_position', 10000.0),
                max_total_position=pm_data.get('max_total_position', 50000.0),
            )
        
        # 账户余额管理
        if 'balance_management' in data:
            bm_data = data['balance_management']
            self.config.risk_control.balance_management = BalanceManagementConfig(
                min_balance_warning=bm_data.get('min_balance_warning', 1000.0),
                min_balance_close_position=bm_data.get('min_balance_close_position', 500.0),
                check_interval=bm_data.get('check_interval', 60),
            )
        
        # 网络故障处理
        if 'network_failure' in data:
            nf_data = data['network_failure']
            self.config.risk_control.network_failure = NetworkFailureConfig(
                enabled=nf_data.get('enabled', True),
                reconnect_interval=nf_data.get('reconnect_interval', 30),
                max_reconnect_attempts=nf_data.get('max_reconnect_attempts', 10),
                auto_resume=nf_data.get('auto_resume', True),
            )
        
        # 交易所维护检测
        if 'exchange_maintenance' in data:
            em_data = data['exchange_maintenance']
            self.config.risk_control.exchange_maintenance = ExchangeMaintenanceConfig(
                enabled=em_data.get('enabled', True),
                check_interval=em_data.get('check_interval', 60),
                maintenance_error_codes=em_data.get('maintenance_error_codes', [503, 502]),
                auto_resume=em_data.get('auto_resume', True),
            )
        
        # 脚本崩溃恢复
        # 价格异常检测
        if 'price_anomaly' in data:
            pa_data = data['price_anomaly']
            self.config.risk_control.price_anomaly = PriceAnomalyConfig(
                enabled=pa_data.get('enabled', True),
                max_price_change_rate=pa_data.get('max_price_change_rate', 0.05),
                check_window=pa_data.get('check_window', 5),
            )
        
        # 订单执行异常检测
        if 'order_anomaly' in data:
            oa_data = data['order_anomaly']
            self.config.risk_control.order_anomaly = OrderAnomalyConfig(
                enabled=oa_data.get('enabled', True),
                max_order_wait_time=oa_data.get('max_order_wait_time', 300),
                auto_cancel_timeout=oa_data.get('auto_cancel_timeout', True),
            )
        
        # 资金费率异常检测
        if 'funding_rate_anomaly' in data:
            fra_data = data['funding_rate_anomaly']
            self.config.risk_control.funding_rate_anomaly = FundingRateAnomalyConfig(
                enabled=fra_data.get('enabled', True),
                max_funding_rate_change=fra_data.get('max_funding_rate_change', 0.01),
            )
        
        # 持仓时间限制
        if 'position_duration' in data:
            pd_data = data['position_duration']
            self.config.risk_control.position_duration = PositionDurationConfig(
                enabled=pd_data.get('enabled', True),
                max_position_duration=pd_data.get('max_position_duration', 168),
                auto_close_on_timeout=pd_data.get('auto_close_on_timeout', True),
            )
        
        # 单次交易风险限制
        if 'single_trade_risk' in data:
            str_data = data['single_trade_risk']
            self.config.risk_control.single_trade_risk = SingleTradeRiskConfig(
                enabled=str_data.get('enabled', True),
                max_single_trade_loss=str_data.get('max_single_trade_loss', 1000.0),
            )
        
        # 每日交易次数限制
        if 'daily_trade_limit' in data:
            dtl_data = data['daily_trade_limit']
            self.config.risk_control.daily_trade_limit = DailyTradeLimitConfig(
                enabled=dtl_data.get('enabled', True),
                max_daily_trades=dtl_data.get('max_daily_trades', 100),
            )
        
        # 交易对风险限制
        if 'symbol_risk' in data:
            sr_data = data['symbol_risk']
            self.config.risk_control.symbol_risk = SymbolRiskConfig(
                disabled_symbols=sr_data.get('disabled_symbols', []),
                high_risk_symbols=sr_data.get('high_risk_symbols', []),
            )
        
        # 数据一致性检查
        if 'data_consistency' in data:
            dc_data = data['data_consistency']
            self.config.risk_control.data_consistency = DataConsistencyConfig(
                enabled=dc_data.get('enabled', True),
                check_interval=dc_data.get('check_interval', 300),
                auto_fix=dc_data.get('auto_fix', True),
            )
    
    def _load_defaults(self):
        """加载默认配置"""
        # 使用默认配置（已在ArbitrageUnifiedConfig中定义）
        logger.info("✅ [配置管理] 使用默认配置")
    
    def get_decision_config(self) -> DecisionConfig:
        """获取套利决策配置"""
        return self.config.decision
    
    def get_execution_config(self) -> ExecutionConfig:
        """获取套利执行配置"""
        return self.config.execution
    
    def get_risk_control_config(self) -> RiskControlConfig:
        """获取风险控制配置"""
        return self.config.risk_control
    
    def get_unified_config(self) -> ArbitrageUnifiedConfig:
        """获取统一配置对象"""
        return self.config
    
    def validate(self) -> bool:
        """
        验证配置有效性
        
        Returns:
            配置是否有效
        """
        errors = []
        
        # 验证决策配置
        thresholds = self.config.decision.thresholds
        if thresholds.spread_arbitrage_threshold <= 0:
            errors.append("价差套利触发阈值必须大于0")
        if thresholds.funding_rate_diff_threshold <= 0:
            errors.append("资金费率差套利触发阈值必须大于0")
        if thresholds.history_hours <= 0:
            errors.append("历史记录数据小时数必须大于0")
        if thresholds.unfavorable_funding_rate_duration_minutes <= 0:
            errors.append("资金费率不利持续时间阈值必须大于0")
        if thresholds.loss_stop_threshold <= 0:
            errors.append("价差亏损百分比阈值必须大于0")
        
        # 验证执行配置
        if self.config.execution.default_order_mode not in ['limit_market', 'market_market']:
            errors.append(f"默认下单模式无效: {self.config.execution.default_order_mode}")
        
        if self.config.execution.order_execution.partial_fill_strategy not in ['continue', 'cancel']:
            errors.append(f"部分成交处理策略无效: {self.config.execution.order_execution.partial_fill_strategy}")
        
        # 验证风险控制配置
        if self.config.risk_control.position_management.max_single_token_position <= 0:
            errors.append("单一代币最大持仓必须大于0")
        if self.config.risk_control.position_management.max_total_position <= 0:
            errors.append("所有代币最大持仓必须大于0")
        if self.config.risk_control.balance_management.min_balance_close_position >= \
           self.config.risk_control.balance_management.min_balance_warning:
            errors.append("余额不足平仓阈值必须小于警告阈值")
        
        # 验证手续费配置
        fee_config = self.config.execution.exchange_fee_config
        if 'default' not in fee_config:
            errors.append("必须配置 default 手续费参数")
        for exchange_name, config in fee_config.items():
            if config.limit_fee_rate < 0 or config.market_fee_rate < 0:
                errors.append(
                    f"{exchange_name} 手续费率必须大于等于0 "
                    f"(limit={config.limit_fee_rate}, market={config.market_fee_rate})"
                )
        
        if errors:
            for error in errors:
                logger.error(f"❌ [配置管理] 配置验证失败: {error}")
            return False
        
        logger.info("✅ [配置管理] 配置验证通过")
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        """将配置转换为字典"""
        return asdict(self.config)
    
    def save_to_file(self, output_path: Path):
        """保存配置到文件"""
        try:
            data = self.to_dict()
            with open(output_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            logger.info(f"✅ [配置管理] 配置已保存到: {output_path}")
        except Exception as e:
            logger.error(f"❌ [配置管理] 保存配置失败: {e}", exc_info=True)
            raise
    
    def set_on_config_changed(self, callback: Callable[[ArbitrageUnifiedConfig], None]):
        """
        设置配置变更回调
        
        Args:
            callback: 配置变更时的回调函数
        """
        self._on_config_changed = callback
    
    async def start_hot_reload(self):
        """启动热更新任务"""
        if not self.enable_hot_reload or not self.config_path:
            return
        
        if self._running:
            logger.warning("⚠️  [配置管理] 热更新任务已在运行")
            return
        
        self._running = True
        self._hot_reload_task = asyncio.create_task(self._hot_reload_loop())
        logger.info(f"✅ [配置管理] 热更新任务已启动（检查间隔: {self.hot_reload_interval}秒）")
    
    async def stop_hot_reload(self):
        """停止热更新任务"""
        if not self._running:
            return
        
        self._running = False
        if self._hot_reload_task:
            self._hot_reload_task.cancel()
            try:
                await self._hot_reload_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ [配置管理] 热更新任务已停止")
    
    async def _hot_reload_loop(self):
        """热更新循环"""
        while self._running:
            try:
                await asyncio.sleep(self.hot_reload_interval)
                
                if not self.config_path or not self.config_path.exists():
                    continue
                
                # 检查文件修改时间
                current_mtime = self.config_path.stat().st_mtime
                
                if self._last_modified_time is None:
                    self._last_modified_time = current_mtime
                    continue
                
                if current_mtime > self._last_modified_time:
                    logger.info("🔄 [配置管理] 检测到配置文件变更，重新加载...")
                    
                    # 备份当前配置
                    old_config = self.config
                    
                    # 重新加载配置
                    try:
                        self._load_from_file()
                        self._last_modified_time = current_mtime
                        
                        # 验证配置
                        if self.validate():
                            logger.info("✅ [配置管理] 配置热更新成功")
                            
                            # 调用回调函数
                            if self._on_config_changed:
                                try:
                                    self._on_config_changed(self.config)
                                except Exception as e:
                                    logger.error(f"❌ [配置管理] 配置变更回调执行失败: {e}", exc_info=True)
                        else:
                            logger.warning("⚠️  [配置管理] 配置验证失败，使用旧配置")
                            self.config = old_config
                    except Exception as e:
                        logger.error(f"❌ [配置管理] 配置热更新失败: {e}，使用旧配置", exc_info=True)
                        self.config = old_config
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ [配置管理] 热更新循环出错: {e}", exc_info=True)
                await asyncio.sleep(self.hot_reload_interval)

