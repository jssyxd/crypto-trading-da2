"""
套利系统统一配置模块

职责：
- 定义套利决策、执行、风险控制等所有配置的数据结构
- 提供配置验证和访问接口
- 支持配置热更新（可选）

注意：此模块仅用于套利监控系统v2，不影响其他系统（网格系统等）
"""

from typing import List, Dict, Any, Optional
from pathlib import Path
from dataclasses import dataclass, field
from decimal import Decimal


# ============================================================================
# 系统运行模式配置
# ============================================================================

@dataclass
class SystemModeConfig:
    """系统运行模式配置"""
    # 监控模式（Monitor Only Mode）
    # True: 只监控，不执行真实订单（所有套利流程正常运行，但不下单）
    # False: 正常模式，执行真实订单
    monitor_only: bool = True  # 🔥 默认开启监控模式，避免误操作
    
    # 数据新鲜度配置（Data Freshness）
    # 在进行价差分析时，只使用新鲜度在此阈值内的交易所数据
    # 目的：防止使用过期价格数据进行套利决策，避免虚假套利信号
    data_freshness_seconds: float = 3.0  # 数据新鲜度阈值（秒），默认3秒


# ============================================================================
# 套利决策配置
# ============================================================================

@dataclass
class DecisionThresholdsConfig:
    """套利决策阈值配置"""
    # 价差套利阈值
    spread_arbitrage_threshold: float = 0.02  # 价差套利触发阈值（百分比）
    spread_persistence_seconds: int = 1  # 价差连续满足秒数，默认1秒（即可立即触发）
    
    # 资金费率套利阈值
    funding_rate_diff_threshold: float = 0.01  # 资金费率差套利触发阈值（百分比）
    
    # 可忽视阈值
    ignorable_spread_threshold: float = 0.05  # 可忽视的差价百分比阈值（百分比）
    ignorable_funding_rate_diff_threshold: float = 0.01  # 可忽视的资金费率差价百分比阈值（百分比）
    
    # 历史数据时间范围
    history_hours: int = 24  # 历史记录数据的小时数量（小时）
    
    # 资金费率不利持续时间
    unfavorable_funding_rate_duration_minutes: int = 60  # 资金费率不利持续时间阈值（分钟）
    
    # 平仓阈值
    profit_target_threshold: float = 0.02  # 价差盈利百分比阈值（百分比）
    loss_stop_threshold: float = 0.3  # 价差亏损百分比阈值（百分比）
    
    # 资金费率年化阈值
    funding_rate_annual_threshold: float = 10.0  # 资金费率年化阈值（百分比/年）
    
    # 🔥 稳定性阈值配置（可自定义调整）
    # ========== 资金费率差稳定性判断参数 ==========
    funding_rate_stability_threshold: float = 0.5  # 波动系数阈值（标准差/平均值）
    funding_rate_min_threshold: float = 0.01  # 资金费率差最小阈值（用于持续时间判断）
    funding_rate_duration_ratio: float = 0.8  # 持续时间占比阈值（80%的时间要满足）
    funding_rate_extreme_ratio: float = 0.1  # 极端波动次数占比阈值（≤10%）
    funding_rate_extreme_multiplier: float = 1.5  # 极端波动倍数（超过平均变化的倍数）
    
    # ========== 价差稳定性判断参数 ==========
    spread_stability_threshold: float = 0.5  # 波动系数阈值（标准差/绝对值平均）
    spread_extreme_ratio: float = 0.15  # 极端波动次数占比阈值（≤15%）
    spread_extreme_multiplier: float = 2.0  # 极端波动倍数（超过平均变化的倍数）
    
    # ========== 通用参数 ==========
    min_data_points_for_stability: int = 10  # 稳定性判断最少数据点要求


@dataclass
class DecisionConfig:
    """套利决策配置"""
    thresholds: DecisionThresholdsConfig = field(default_factory=DecisionThresholdsConfig)
    
    # 决策开关
    enabled: bool = True  # 是否启用套利决策
    enable_spread_arbitrage: bool = True  # 是否启用价差套利
    enable_funding_rate_arbitrage: bool = True  # 是否启用资金费率套利


# ============================================================================
# 套利执行配置
# ============================================================================

@dataclass
class QuantityConfig:
    """数量配置（代币本位）"""
    single_order_quantity: float = 0.1       # 单笔下单数量（代币本位）
    max_position_quantity: float = 0.2       # 最大持仓数量（代币本位）
    quantity_precision: int = 4              # 数量精度（小数位数，由配置指定）


@dataclass
class ExchangeOrderModeConfig:
    """交易所下单模式配置"""
    order_mode: str = "market"  # limit 或 market
    priority: int = 0  # 🔥 下单优先级（数字越大越先下单）
    limit_price_offset: float = 0.001  # 限价单价格偏移（百分比）
    use_tick_precision: bool = False  # 是否使用tick精度定价
    force_sequential_limit: bool = False  # 🔥 强制顺序执行：即使双方都是limit，也采用limit+market模式
    force_limit_when_second_leg: bool = False  # 🔥 限价+市价模式中若成为第二腿则强制使用激进限价
    reuse_signal_price_when_second_leg: bool = False  # 🔧 第二腿激进限价是否复用信号价（默认重新拉最新盘口价）
    smart_second_leg_price: bool = False  # 🔧 智能盘口价格切换（根据第一腿表现自动选择价格来源）
    
    # 🔥 兼容旧配置格式
    buy_mode: Optional[str] = None  # 已废弃，保留兼容性
    sell_mode: Optional[str] = None  # 已废弃，保留兼容性


@dataclass
class ExchangeFeeConfig:
    """交易所手续费配置"""
    limit_fee_rate: float = 0.0001  # 限价单（maker）手续费，默认万分之1
    market_fee_rate: float = 0.0003  # 市价单（taker）手续费，默认万分之3


@dataclass
class ExchangeRateLimitConfig:
    """交易所限速配置"""
    max_concurrent_orders: int = 1          # 同时允许的最大并发下单数
    min_interval_ms: int = 0                # 连续两次下单的最小时间间隔（毫秒）
    rate_limit_cooldown_ms: int = 1000      # 触发限流后的额外冷却时间（毫秒）


@dataclass
class OrderExecutionConfig:
    """订单执行配置"""
    limit_order_timeout: int = 60  # 限价订单等待时间（秒）
    lighter_market_order_timeout: Optional[int] = None  # lighter市价订单自定义等待时间（秒）
    max_retry_count: int = 3  # 订单重试次数
    partial_fill_strategy: str = "continue"  # continue: 继续执行, cancel: 取消订单
    round_pause_seconds: float = 60.0  # 每轮完成后的等待时间（秒）
    
    # 滑点保护
    slippage_protection_enabled: bool = True
    max_slippage: float = 0.005  # 最大允许滑点（百分比）

    # 双限价模式
    enable_dual_limit_mode: bool = False  # 同一套利腿允许双限价并行
    dual_limit_retry_initial_delay: int = 30  # 双限价全未成交时的初始避让秒数
    dual_limit_retry_max_delay: int = 600  # 双限价避让的最大间隔
    dual_limit_retry_backoff_factor: float = 2.0  # 双限价避让指数倍数


@dataclass
class ExecutionRiskControlConfig:
    """执行模块风险控制配置"""
    max_order_size: float = 1.0  # 最大订单数量
    min_order_size: float = 0.001  # 最小订单数量
    
    # 价格变化检查
    price_change_check_enabled: bool = True
    max_price_change: float = 0.01  # 最大允许价格变化（百分比）


@dataclass
class ExecutionConfig:
    """套利执行配置"""
    default_order_mode: str = "limit_market"  # limit_market 或 market_market
    
    # 🔥 数量配置（按代币，USDC计价）
    quantity_config: Dict[str, QuantityConfig] = field(default_factory=dict)
    
    # 交易所下单模式配置
    exchange_order_modes: Dict[str, ExchangeOrderModeConfig] = field(default_factory=dict)
    
    # 交易所手续费配置
    exchange_fee_config: Dict[str, ExchangeFeeConfig] = field(
        default_factory=lambda: {'default': ExchangeFeeConfig()}
    )

    # 交易所限速配置
    exchange_rate_limits: Dict[str, ExchangeRateLimitConfig] = field(default_factory=dict)
    
    # 订单执行参数
    order_execution: OrderExecutionConfig = field(default_factory=OrderExecutionConfig)
    
    # 风险控制
    risk_control: ExecutionRiskControlConfig = field(default_factory=ExecutionRiskControlConfig)


# ============================================================================
# 风险控制配置
# ============================================================================

@dataclass
class PositionManagementConfig:
    """仓位管理配置（兜底数量限制）"""
    max_single_token_position: float = 10.0   # 单一代币最大持仓数量（作为默认/兜底）
    max_total_position: float = 30.0  # 所有代币最大持仓数量（绝对值之和）


@dataclass
class BalanceManagementConfig:
    """账户余额管理配置"""
    min_balance_warning: float = 1000.0  # 余额不足警告阈值（USDC价值）
    min_balance_close_position: float = 500.0  # 余额不足平仓阈值（USDC价值）
    check_interval: int = 60  # 余额检查间隔（秒）


@dataclass
class NetworkFailureConfig:
    """网络故障处理配置"""
    enabled: bool = True
    reconnect_interval: int = 30  # 重连检测间隔（秒）
    max_reconnect_attempts: int = 10  # 最大重连次数
    auto_resume: bool = True  # 网络恢复后自动恢复


@dataclass
class ExchangeMaintenanceConfig:
    """交易所维护检测配置"""
    enabled: bool = True
    check_interval: int = 60  # 维护检测间隔（秒）
    maintenance_error_codes: List[int] = field(default_factory=lambda: [503, 502])
    auto_resume: bool = True  # 恢复后自动恢复


@dataclass
class PriceAnomalyConfig:
    """价格异常检测配置"""
    enabled: bool = True
    max_price_change_rate: float = 0.05  # 最大允许价格变化率（5%）
    check_window: int = 5  # 检查时间窗口（分钟）


@dataclass
class OrderAnomalyConfig:
    """订单执行异常检测配置"""
    enabled: bool = True
    max_order_wait_time: int = 300  # 订单最大等待时间（秒）
    auto_cancel_timeout: bool = True  # 超时自动取消


@dataclass
class FundingRateAnomalyConfig:
    """资金费率异常检测配置"""
    enabled: bool = True
    max_funding_rate_change: float = 0.01  # 最大允许资金费率变化（1%）


@dataclass
class PositionDurationConfig:
    """持仓时间限制配置"""
    enabled: bool = True
    max_position_duration: int = 168  # 最大持仓时间（小时，7天）
    auto_close_on_timeout: bool = True  # 超时自动平仓


@dataclass
class SingleTradeRiskConfig:
    """单次交易风险限制配置"""
    enabled: bool = True
    max_single_trade_loss: float = 1000.0  # 单次交易最大亏损（USDC价值）


@dataclass
class DailyTradeLimitConfig:
    """每日交易次数限制配置"""
    enabled: bool = True
    max_daily_trades: int = 100  # 每日最大交易次数


@dataclass
class SymbolRiskConfig:
    """交易对风险限制配置"""
    disabled_symbols: List[str] = field(default_factory=list)  # 暂停交易对列表
    high_risk_symbols: List[str] = field(default_factory=list)  # 高风险交易对列表


@dataclass
class DataConsistencyConfig:
    """数据一致性检查配置"""
    enabled: bool = True
    check_interval: int = 300  # 一致性检查间隔（秒）
    auto_fix: bool = True  # 自动修复不一致数据


@dataclass
class RiskControlConfig:
    """全局风险控制配置"""
    # 仓位管理
    position_management: PositionManagementConfig = field(default_factory=PositionManagementConfig)
    
    # 账户余额管理
    balance_management: BalanceManagementConfig = field(default_factory=BalanceManagementConfig)
    
    # 网络故障处理
    network_failure: NetworkFailureConfig = field(default_factory=NetworkFailureConfig)
    
    # 交易所维护检测
    exchange_maintenance: ExchangeMaintenanceConfig = field(default_factory=ExchangeMaintenanceConfig)
    
    # 价格异常检测
    price_anomaly: PriceAnomalyConfig = field(default_factory=PriceAnomalyConfig)
    
    # 订单执行异常检测
    order_anomaly: OrderAnomalyConfig = field(default_factory=OrderAnomalyConfig)
    
    # 资金费率异常检测
    funding_rate_anomaly: FundingRateAnomalyConfig = field(default_factory=FundingRateAnomalyConfig)
    
    # 持仓时间限制
    position_duration: PositionDurationConfig = field(default_factory=PositionDurationConfig)
    
    # 单次交易风险限制
    single_trade_risk: SingleTradeRiskConfig = field(default_factory=SingleTradeRiskConfig)
    
    # 每日交易次数限制
    daily_trade_limit: DailyTradeLimitConfig = field(default_factory=DailyTradeLimitConfig)
    
    # 交易对风险限制
    symbol_risk: SymbolRiskConfig = field(default_factory=SymbolRiskConfig)
    
    # 数据一致性检查
    data_consistency: DataConsistencyConfig = field(default_factory=DataConsistencyConfig)


# ============================================================================
# 分段套利模式配置
# ============================================================================

@dataclass
class GridConfig:
    """分段套利网格配置"""
    # 初始开仓阈值（第1段）
    initial_spread_threshold: float = 0.05  # %，价差达到此值时开第1段仓位
    
    # 网格步长（每段价差间隔）
    grid_step: float = 0.03  # %，价差每增加此值，开下一段仓位
    
    # 最大段数（风控）
    max_segments: int = 5  # 最多开5段仓位
    
    # 每段数量比例
    segment_quantity_ratio: float = 1.0  # 每段数量 = 基础数量 * 比例
    
    # 单次下单比例（拆单用），1.0表示一次下完，0.1表示拆成10笔
    segment_partial_order_ratio: float = 1.0
    
    # 单笔最小下单数量（代币本位），防止拆得过小
    min_partial_order_quantity: float = 0.0
    
    # 每段目标收益（非对称平仓模式使用）
    profit_per_segment: float = 0.02  # %，每段平仓时的目标收益
    
    # 是否使用对称分段平仓（价差回落与开仓步长一致）
    use_symmetric_close: bool = False
    scalp_profit_threshold: float = 0.0  # 剥头皮模式下触发全平的收益阈值
    
    # 🔥 剥头皮模式配置
    scalping_enabled: bool = False  # 是否启用剥头皮模式
    scalping_trigger_segment: int = 4  # 触发剥头皮的段数
    scalping_profit_threshold: float = 0.05  # 剥头皮止盈阈值
    
    # 价差连续满足秒数
    spread_persistence_seconds: int = 3  # 秒，价差连续满足N秒才触发开仓


@dataclass
class SegmentedFundingRateConfig:
    """分段模式资金费率配置"""
    # 开仓时资金费率要求
    open_funding_rate_favorable: bool = True  # 开仓时资金费率必须有利或不亏损
    
    # 平仓时资金费率阈值
    close_funding_rate_threshold: float = 0.01  # %，资金费率支付超过此值则平仓
    
    # 资金费率不利持续时间
    unfavorable_duration_minutes: int = 60  # 分钟，不利持续此时间才触发平仓
    
    # 资金费率年化阈值
    funding_rate_annual_threshold: float = 10.0  # %/年，年化超过此值视为过度不利


@dataclass
class ForceCloseConfig:
    """强制平仓配置"""
    # 价差归零平仓阈值
    zero_spread_threshold: float = 0.005  # %，价差 ≤ 此值时全部平仓
    
    # 价差为负平仓开关
    close_on_negative_spread: bool = True  # 价差为负时是否全部平仓


@dataclass
class SegmentedRiskControlConfig:
    """分段模式风控配置"""
    # 最大持仓数量限制
    max_total_quantity: float = 10.0  # 所有段总数量不超过此值
    
    # 最小可用保证金比例
    min_available_margin_ratio: float = 0.3  # 可用保证金/总权益 ≥ 30%
    
    # 紧急止损阈值（所有段整体亏损）
    emergency_stop_loss_threshold: float = 1.0  # %，整体亏损 ≥ 1% 全部平仓


@dataclass
class SegmentedDecisionConfig:
    """分段套利决策配置"""
    # 是否启用分段模式
    enable: bool = True
    
    # 网格配置
    grid_config: GridConfig = field(default_factory=GridConfig)
    
    # 资金费率配置（用于开仓和平仓判断，不做资金费率套利）
    funding_rate_config: SegmentedFundingRateConfig = field(default_factory=SegmentedFundingRateConfig)
    
    # 强制平仓配置
    force_close_config: ForceCloseConfig = field(default_factory=ForceCloseConfig)
    
    # 风控配置
    risk_control: SegmentedRiskControlConfig = field(default_factory=SegmentedRiskControlConfig)
    
    # 手续费配置开关
    deduct_fees: bool = True  # 是否扣除手续费计算净价差


# ============================================================================
# 统一配置类
# ============================================================================

@dataclass
class ArbitrageUnifiedConfig:
    """套利系统统一配置"""
    # 系统运行模式配置
    system_mode: SystemModeConfig = field(default_factory=SystemModeConfig)
    
    # 套利决策配置
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    
    # 套利执行配置
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    
    # 风险控制配置
    risk_control: RiskControlConfig = field(default_factory=RiskControlConfig)


@dataclass
class SegmentedArbitrageConfig:
    """分段套利系统配置（独立配置）"""
    # 系统运行模式配置
    system_mode: SystemModeConfig = field(default_factory=SystemModeConfig)
    
    # 分段套利决策配置
    decision: SegmentedDecisionConfig = field(default_factory=SegmentedDecisionConfig)
    
    # 套利执行配置（复用传统模式的执行配置）
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    
    # 风险控制配置（复用传统模式的风控配置）
    risk_control: RiskControlConfig = field(default_factory=RiskControlConfig)

