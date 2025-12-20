"""
分段套利系统 - 多交易对独立配置管理器

职责：
1. 加载配置文件（支持全局默认 + 交易对特定配置）
2. 提供配置查询接口
3. 自动合并默认值和交易对配置
"""

from dataclasses import dataclass, field
from typing import Dict, Optional
from decimal import Decimal
from pathlib import Path
import yaml
import logging

logger = logging.getLogger(__name__)


@dataclass
class GridConfig:
    """网格配置"""
    # 必填参数（无默认值）
    initial_spread_threshold: float
    grid_step: float
    max_segments: int
    
    segment_quantity_ratio: float  # 🔥 每段数量比例
    segment_partial_order_ratio: float  # 🔥 已废弃：用于判断是否拆单（>=1.0不拆，<1.0拆单）
    min_partial_order_quantity: float  # 🔥 拆单时的单笔数量
    
    profit_per_segment: float  # 🔥 每段目标收益
    
    use_symmetric_close: bool  # 🔥 是否使用对称平仓
    scalp_profit_threshold: float  # 🔥 剥头皮止盈阈值
    
    scalping_enabled: bool
    scalping_trigger_segment: int
    scalping_profit_threshold: float
    
    spread_persistence_seconds: float
    strict_persistence_check: bool  # 🔥 严格持续性检查：True=每秒都必须满足, False=每秒至少一次满足即可
    
    # 可选参数（有默认值）
    split_order_size: Optional[float] = None  # 🔥 新参数：单笔订单数量（优先使用）
    require_orderbook_liquidity: bool = False  # 🔥 对手盘深度校验开关
    min_orderbook_quantity: Optional[float] = None  # 🔥 对手盘最小深度要求（绝对数量）
    slippage_tolerance: Optional[float] = None  # 🔥 市价单最大滑点（小数，例如0.0005=0.05%）
    price_stability_window_seconds: Optional[float] = None  # 🔥 盘口价格稳定性窗口（秒）
    price_stability_threshold_pct: Optional[float] = None  # 🔥 价格波动阈值（百分比，比如0.01=0.01%）
    limit_price_offset: Optional[float] = None  # 🔥 限价单价格偏移（绝对数值）
    max_local_orderbook_spread_pct: Optional[float] = None  # 🔥 单交易对自身bid-ask点差上限（百分比）


@dataclass
class QuantityConfig:
    """数量配置"""
    base_quantity: Decimal
    quantity_mode: str  # "fixed" or "value"
    target_value_usdc: Decimal
    quantity_precision: int
    min_order_size: Decimal = Decimal('0')
    exchange_min_order_qty: Dict[str, Decimal] = field(default_factory=dict)


@dataclass
class RiskConfig:
    """风控配置"""
    max_position_value: Decimal
    max_loss_percent: float
    enable_funding_rate_risk: bool = True
    max_unfavorable_funding_hours: int = 8
    funding_rate_diff_threshold: float = 0.01


@dataclass
class SymbolConfig:
    """单个交易对的完整配置"""
    symbol: str
    grid_config: GridConfig
    quantity_config: QuantityConfig
    risk_config: RiskConfig
    
    def __str__(self) -> str:
        return (
            f"SymbolConfig({self.symbol}): "
            f"grid_step={self.grid_config.grid_step}%, "
            f"max_segments={self.grid_config.max_segments}, "
            f"scalping={'ON' if self.grid_config.scalping_enabled else 'OFF'}"
        )


class SegmentedConfigManager:
    """分段配置管理器（支持多交易对独立配置）"""
    
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or Path("config/arbitrage/arbitrage_segmented.yaml")
        self.default_config: Optional[SymbolConfig] = None
        self.symbol_configs: Dict[str, SymbolConfig] = {}
        self._config_aliases: Dict[str, str] = {}
        self.system_mode: Dict = {}
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        # 加载系统模式配置
        self.system_mode = data.get('system_mode', {})
        
        # 加载默认配置
        default = data.get('default_config', {})
        self.default_config = self._build_symbol_config(
            symbol="__DEFAULT__",
            config_data=default
        )
        
        # 加载各交易对配置
        symbol_configs = data.get('symbol_configs', {})
        for symbol, config_data in symbol_configs.items():
            symbol_key = symbol.upper()
            
            # 🔧 检查 enabled 开关
            if not config_data.get('enabled', True):
                logger.info(f"⏸️  跳过已禁用的交易对: {symbol_key}")
                continue
            
            try:
                self.symbol_configs[symbol_key] = self._merge_with_default(
                    symbol=symbol_key,
                    config_data=config_data
                )
                logger.info(f"✅ 加载交易对配置: {self.symbol_configs[symbol_key]}")
            except Exception as e:
                logger.error(f"❌ 加载 {symbol} 配置失败: {e}", exc_info=True)
        
        logger.info(
            f"✅ 配置加载完成: "
            f"默认配置已设置, "
            f"{len(self.symbol_configs)} 个交易对有独立配置"
        )
    
    def _build_symbol_config(
        self,
        symbol: str,
        config_data: dict
    ) -> SymbolConfig:
        """构建单个交易对配置"""
        grid_data = config_data.get('grid_config', {})
        qty_data = config_data.get('quantity_config', {})
        risk_data = config_data.get('risk_config', {})
        
        # 🔀 split_order_size 现已放入 quantity_config，为兼容旧配置同时支持新位置
        raw_split_size = qty_data.get('split_order_size')
        if raw_split_size is None:
            raw_split_size = grid_data.get('split_order_size')
        split_order_size = float(raw_split_size) if raw_split_size is not None else None

        exchange_min_map: Dict[str, Decimal] = {}
        raw_min_map = qty_data.get('min_exchange_order_qty') or {}
        if isinstance(raw_min_map, dict):
            for exch_name, value in raw_min_map.items():
                if value is None:
                    continue
                try:
                    exchange_min_map[str(exch_name).lower()] = Decimal(str(value))
                except Exception:
                    logger.warning(
                        "⚠️ [配置] %s.%s min_exchange_order_qty[%s]=%s 无法解析为Decimal，已忽略",
                        symbol,
                        'quantity_config',
                        exch_name,
                        value
                    )
        
        limit_price_offset_value: Optional[float] = None
        if grid_data.get('limit_price_offset_abs') is not None:
            try:
                limit_price_offset_value = float(grid_data.get('limit_price_offset_abs'))
            except (TypeError, ValueError):
                logger.warning(
                    "⚠️ [配置] %s.grid_config.limit_price_offset_abs=%s 无法解析为浮点数，已忽略",
                    symbol,
                    grid_data.get('limit_price_offset_abs'),
                )
                limit_price_offset_value = None
        elif grid_data.get('limit_price_offset') is not None:
            try:
                limit_price_offset_value = float(grid_data.get('limit_price_offset'))
                logger.debug(
                    "ℹ️ [配置] %s.grid_config.limit_price_offset 已被更名为 limit_price_offset_abs，"
                    "暂时兼容旧字段",
                    symbol,
                )
            except (TypeError, ValueError):
                logger.warning(
                    "⚠️ [配置] %s.grid_config.limit_price_offset=%s 无法解析为浮点数，已忽略",
                    symbol,
                    grid_data.get('limit_price_offset'),
                )
                limit_price_offset_value = None

        return SymbolConfig(
            symbol=symbol,
            grid_config=GridConfig(
                initial_spread_threshold=float(grid_data['initial_spread_threshold']),
                grid_step=float(grid_data['grid_step']),
                max_segments=int(grid_data['max_segments']),
                segment_quantity_ratio=float(grid_data.get('segment_quantity_ratio', 1.0)),  # 🔥 添加
                segment_partial_order_ratio=float(grid_data.get('segment_partial_order_ratio', 1.0)),
                min_partial_order_quantity=float(grid_data.get('min_partial_order_quantity', 0.0)),
                split_order_size=split_order_size,
                profit_per_segment=float(grid_data.get('profit_per_segment', 0.02)),  # 🔥 添加
                use_symmetric_close=bool(grid_data.get('use_symmetric_close', False)),
                scalp_profit_threshold=float(grid_data.get('scalp_profit_threshold', 0.0)),
                scalping_enabled=bool(grid_data.get('scalping_enabled', False)),
                scalping_trigger_segment=int(grid_data.get('scalping_trigger_segment', 4)),
                scalping_profit_threshold=float(grid_data.get('scalping_profit_threshold', 0.05)),
                spread_persistence_seconds=float(grid_data.get('spread_persistence_seconds', 3)),
                strict_persistence_check=bool(grid_data.get('strict_persistence_check', False)),  # 🔥 默认false（宽松模式）
                require_orderbook_liquidity=bool(grid_data.get('require_orderbook_liquidity', False)),
                min_orderbook_quantity=float(grid_data.get('min_orderbook_quantity')) if grid_data.get('min_orderbook_quantity') is not None else None,
                slippage_tolerance=float(grid_data.get('slippage_tolerance')) if grid_data.get('slippage_tolerance') is not None else None,
                price_stability_window_seconds=float(grid_data.get('price_stability_window_seconds')) if grid_data.get('price_stability_window_seconds') is not None else None,
                price_stability_threshold_pct=float(grid_data.get('price_stability_threshold_pct')) if grid_data.get('price_stability_threshold_pct') is not None else None,
                limit_price_offset=limit_price_offset_value,
                max_local_orderbook_spread_pct=float(grid_data.get('max_local_orderbook_spread_pct')) if grid_data.get('max_local_orderbook_spread_pct') is not None else None,
            ),
            quantity_config=QuantityConfig(
                base_quantity=Decimal(str(qty_data['base_quantity'])),
                quantity_mode=qty_data['quantity_mode'],
                target_value_usdc=Decimal(str(qty_data['target_value_usdc'])),
                quantity_precision=int(qty_data.get('quantity_precision', 2)),
                min_order_size=Decimal(str(qty_data.get('min_order_size', 0))) if qty_data.get('min_order_size') is not None else Decimal('0'),
                exchange_min_order_qty=exchange_min_map,
            ),
            risk_config=RiskConfig(
                max_position_value=Decimal(str(risk_data['max_position_value'])),
                max_loss_percent=float(risk_data['max_loss_percent']),
                enable_funding_rate_risk=bool(risk_data.get('enable_funding_rate_risk', True)),
                max_unfavorable_funding_hours=int(risk_data.get('max_unfavorable_funding_hours', 8)),
                funding_rate_diff_threshold=float(risk_data.get('funding_rate_diff_threshold', 0.01))
            )
        )
    
    def _merge_with_default(
        self,
        symbol: str,
        config_data: dict
    ) -> SymbolConfig:
        """
        将交易对配置与默认配置合并
        交易对配置会覆盖默认值
        """
        # 深拷贝默认配置
        merged = {
            'grid_config': {},
            'quantity_config': {},
            'risk_config': {}
        }
        
        # 复制默认值
        if self.default_config:
            merged['grid_config'] = self.default_config.grid_config.__dict__.copy()
            merged['quantity_config'] = {
                'base_quantity': float(self.default_config.quantity_config.base_quantity),
                'quantity_mode': self.default_config.quantity_config.quantity_mode,
                'target_value_usdc': float(self.default_config.quantity_config.target_value_usdc),
                'quantity_precision': self.default_config.quantity_config.quantity_precision,
                'min_order_size': float(self.default_config.quantity_config.min_order_size),
                'min_exchange_order_qty': dict(
                    getattr(self.default_config.quantity_config, 'exchange_min_order_qty', {}) or {}
                ),
            }
            merged['risk_config'] = {
                'max_position_value': float(self.default_config.risk_config.max_position_value),
                'max_loss_percent': self.default_config.risk_config.max_loss_percent,
                'enable_funding_rate_risk': self.default_config.risk_config.enable_funding_rate_risk,
                'max_unfavorable_funding_hours': self.default_config.risk_config.max_unfavorable_funding_hours,
                'funding_rate_diff_threshold': self.default_config.risk_config.funding_rate_diff_threshold
            }
        
        # 覆盖交易对特定配置
        for key in ['grid_config', 'quantity_config', 'risk_config']:
            if key in config_data:
                merged[key].update(config_data[key])
        
        return self._build_symbol_config(symbol, merged)
    
    def get_config(self, symbol: str) -> SymbolConfig:
        """
        获取交易对配置
        
        Args:
            symbol: 交易对符号
        
        Returns:
            如果有专门配置，返回专门配置；否则返回默认配置
        """
        key = symbol.upper()
        if key in self.symbol_configs:
            return self.symbol_configs[key]
        
        alias_target = self._config_aliases.get(key)
        if alias_target:
            return self.get_config(alias_target)
        
        # 返回默认配置（创建新实例，避免修改原配置）
        if self.default_config:
            return SymbolConfig(
                symbol=symbol,
                grid_config=self.default_config.grid_config,
                quantity_config=self.default_config.quantity_config,
                risk_config=self.default_config.risk_config
            )
        
        raise ValueError(f"未找到 {symbol} 的配置，且默认配置未设置")
    
    def is_symbol_enabled(self, symbol: str) -> bool:
        """
        检查交易对是否启用
        
        Args:
            symbol: 交易对符号
        
        Returns:
            如果交易对在 symbol_configs 中且未被禁用，返回 True；否则返回 False
        """
        key = symbol.upper()
        # 如果在 symbol_configs 中，说明已启用（因为 enabled: false 的已被过滤）
        if key in self.symbol_configs:
            return True
        # 检查别名
        alias_target = self._config_aliases.get(key)
        if alias_target and alias_target in self.symbol_configs:
            return True
        # 不在配置中，返回 False
        return False
    
    def get_all_configured_symbols(self) -> list:
        """获取所有已配置的交易对列表"""
        return list(self.symbol_configs.keys())
    
    def has_custom_config(self, symbol: str) -> bool:
        """检查交易对是否有自定义配置"""
        return symbol.upper() in self.symbol_configs

    def register_config_alias(self, alias: str, target_symbol: str) -> None:
        """
        为未显式配置的交易对注册别名，复用已有配置
        alias: 需要复用配置的symbol，例如多交易所套利生成的 trading_pair_id
        target_symbol: 使用其配置的原始symbol，例如 BTC-USDC-PERP
        """
        if not alias or not target_symbol:
            return
        alias_key = alias.upper()
        target_key = target_symbol.upper()
        if alias_key == target_key:
            return
        self._config_aliases[alias_key] = target_key
    
    def get_grid_map(self, symbol: str) -> dict:
        """
        获取交易对的网格映射表
        
        Returns:
            {
                grid_id: (threshold%, target_position)
            }
        """
        config = self.get_config(symbol)
        grid_map = {}
        
        for i in range(1, config.grid_config.max_segments + 1):
            threshold = config.grid_config.initial_spread_threshold + \
                       (i - 1) * config.grid_config.grid_step
            target_position = i * float(config.quantity_config.base_quantity)
            grid_map[i] = (threshold, target_position)
        
        return grid_map
    
    def get_system_mode(self) -> Dict:
        """获取系统模式配置"""
        return self.system_mode
    
    def is_monitor_only(self) -> bool:
        """是否为监控模式（只监控不下单）"""
        return self.system_mode.get('monitor_only', True)

