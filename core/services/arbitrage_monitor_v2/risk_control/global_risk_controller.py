"""
全局风险控制模块

职责：
- 仓位管理（单一代币最大持仓、所有代币最大持仓）
- 账户余额管理（余额不足警告、余额不足平仓）
- 网络故障处理（自动暂停、重连、恢复）
- 交易所维护检测（检测维护状态、暂停操作、恢复）
- 脚本崩溃恢复（持久化存储、自动恢复）
- 其他风险控制（价格异常、订单异常、数据一致性等）

注意：此模块负责全局风险控制，不涉及具体的套利决策逻辑
"""

import asyncio
import logging
from typing import Dict, List, Optional, Set, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ..config.arbitrage_config import RiskControlConfig, QuantityConfig
from core.adapters.exchanges.models import BalanceData, PositionData
from core.adapters.exchanges.interface import ExchangeInterface

# 数据模型
from ..models import RiskStatus

# 🔥 使用统一日志系统
from core.adapters.exchanges.utils.setup_logging import LoggingConfig

logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file='global_risk_controller.log',
    console_formatter=None,
    file_formatter='detailed',
    level=logging.INFO
)
logger.propagate = False


class GlobalRiskController:
    """全局风险控制器"""
    
    def __init__(
        self,
        risk_config: RiskControlConfig,
        exchange_adapters: Dict[str, ExchangeInterface],
        symbol_quantity_config: Optional[Dict[str, QuantityConfig]] = None,
        allowed_symbols: Optional[Set[str]] = None,
    ):
        """
        初始化全局风险控制器
        
        Args:
            risk_config: 风险控制配置
            exchange_adapters: 交易所适配器字典 {exchange_name: adapter}
        """
        self.config = risk_config
        self.exchange_adapters = exchange_adapters
        self.symbol_quantity_config = symbol_quantity_config or {}
        self.allowed_symbols = {s.upper() for s in (allowed_symbols or set())} or None
        
        # 风险状态
        self.risk_status = RiskStatus()
        
        # 监控任务
        self.monitor_tasks: List[asyncio.Task] = []
        self.running = False
        
        # 回调函数
        self.on_pause: Optional[Callable[[str], None]] = None  # 暂停回调
        self.on_resume: Optional[Callable[[], None]] = None  # 恢复回调
        self.on_close_all_positions: Optional[Callable[[], None]] = None  # 平仓所有仓位回调
        
        # 统计数据
        self.daily_trade_count: Dict[str, int] = {}  # {date: count}
        self.last_trade_date: Optional[str] = None
        
        logger.info("✅ [风险控制] 全局风险控制器初始化完成")
    
    async def start(self):
        """启动风险控制器"""
        if self.running:
            return
        
        self.running = True
        
        # 启动监控任务
        if self.config.balance_management.check_interval > 0:
            self.monitor_tasks.append(
                asyncio.create_task(self._balance_monitor_loop())
            )
        
        if self.config.position_duration.enabled:
            self.monitor_tasks.append(
                asyncio.create_task(self._position_duration_monitor_loop())
            )
        
        if self.config.daily_trade_limit.enabled:
            self.monitor_tasks.append(
                asyncio.create_task(self._daily_trade_limit_monitor_loop())
            )
        
        logger.info("✅ [风险控制] 全局风险控制器已启动")
    
    async def stop(self):
        """停止风险控制器"""
        self.running = False
        
        for task in self.monitor_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        self.monitor_tasks.clear()
        logger.info("🛑 [风险控制] 全局风险控制器已停止")
    
    # ============================================================================
    # 仓位管理
    # ============================================================================
    
    async def check_position_limits(
        self,
        symbol: str,
        exchange: str,
        new_position_quantity: Decimal
    ) -> tuple[bool, Optional[str]]:
        """
        检查仓位限制
        
        Args:
            symbol: 交易对
            exchange: 交易所
            new_position_quantity: 新增持仓数量（代币本位）
        
        Returns:
            (是否允许, 拒绝原因)
        """
        new_position_quantity = abs(Decimal(str(new_position_quantity or 0)))
        
        # 检查单一代币最大持仓（优先使用代币特定配置）
        symbol_limit = self._get_symbol_quantity_limit(symbol)
        if symbol_limit is None:
            fallback = Decimal(str(self.config.position_management.max_single_token_position))
            symbol_limit = fallback if fallback > 0 else None
        
        if symbol_limit is not None:
            current_single_qty = await self._get_single_token_position_quantity(symbol)
            if current_single_qty + new_position_quantity > symbol_limit:
                return False, (
                    f"单一代币持仓超过限制: "
                    f"{current_single_qty + new_position_quantity} > {symbol_limit}"
                )
        
        # 检查所有代币最大持仓
        total_limit = Decimal(str(self.config.position_management.max_total_position))
        if total_limit > 0:
            current_total_qty = await self._get_total_position_quantity()
            if current_total_qty + new_position_quantity > total_limit:
                return False, (
                    f"总持仓超过限制: "
                    f"{current_total_qty + new_position_quantity} > {total_limit}"
                )
        
        return True, None
    
    def _get_symbol_quantity_limit(self, symbol: str) -> Optional[Decimal]:
        """获取代币特定的最大持仓数量"""
        if not self.symbol_quantity_config:
            return None
        
        cfg = self.symbol_quantity_config.get(symbol) or self.symbol_quantity_config.get('default')
        if not cfg:
            return None
        
        max_qty = getattr(cfg, 'max_position_quantity', None)
        if max_qty is None:
            return None
        
        max_qty_decimal = Decimal(str(max_qty))
        return max_qty_decimal if max_qty_decimal > 0 else None
    
    async def _get_single_token_position_quantity(self, symbol: str) -> Decimal:
        """获取单一代币的持仓数量（绝对值累计）"""
        total_quantity = Decimal('0')
        symbol_upper = (symbol or "").upper()
        
        allowed = self.allowed_symbols
        for exchange_name, adapter in self.exchange_adapters.items():
            try:
                positions = await adapter.get_positions()
                for position in positions:
                    pos_symbol = (position.symbol or "").upper()
                    if allowed is not None and pos_symbol not in allowed:
                        continue
                    if pos_symbol == symbol_upper and getattr(position, "size", Decimal('0')) != 0:
                        total_quantity += self._calc_position_quantity(position)
            except Exception as e:
                # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
                # 如果是因为未配置API密钥导致的错误，使用debug级别
                error_msg = str(e)
                if "未配置SignerClient" in error_msg or "未配置API" in error_msg or "无法获取" in error_msg:
                    logger.debug(f"[风险控制] 获取{exchange_name}持仓失败: {e}")
                else:
                    logger.error(f"[风险控制] 获取{exchange_name}持仓失败: {e}", exc_info=True)
        
        return total_quantity
    
    async def _get_total_position_quantity(self) -> Decimal:
        """获取所有代币的总持仓数量（绝对值之和）"""
        total_quantity = Decimal('0')
        allowed = self.allowed_symbols
        
        for exchange_name, adapter in self.exchange_adapters.items():
            try:
                positions = await adapter.get_positions()
                for position in positions:
                    if allowed is not None:
                        pos_symbol = (position.symbol or "").upper()
                        if pos_symbol not in allowed:
                            continue
                    if getattr(position, "size", Decimal('0')) != 0:
                        total_quantity += self._calc_position_quantity(position)
            except Exception as e:
                # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
                # 如果是因为未配置API密钥导致的错误，使用debug级别
                error_msg = str(e)
                if "未配置SignerClient" in error_msg or "未配置API" in error_msg or "无法获取" in error_msg:
                    logger.debug(f"[风险控制] 获取{exchange_name}持仓失败: {e}")
                else:
                    logger.error(f"[风险控制] 获取{exchange_name}持仓失败: {e}", exc_info=True)
        
        return total_quantity
    
    @staticmethod
    def _calc_position_quantity(position: PositionData) -> Decimal:
        """根据position数据安全计算仓位数量（绝对值）"""
        size = getattr(position, "size", None)
        if size is None:
            return Decimal('0')
        try:
            return abs(Decimal(str(size)))
        except Exception:
            return Decimal('0')
    
    # ============================================================================
    # 账户余额管理
    # ============================================================================
    
    async def _balance_monitor_loop(self):
        """余额监控循环"""
        while self.running:
            try:
                await asyncio.sleep(self.config.balance_management.check_interval)
                
                if not self.running:
                    break
                
                await self._check_all_balances()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[风险控制] 余额监控错误: {e}", exc_info=True)
    
    async def _check_all_balances(self):
        """检查所有交易所的余额"""
        low_balance_exchanges = set()
        critical_balance_exchanges = set()
        
        for exchange_name, adapter in self.exchange_adapters.items():
            try:
                balances = await adapter.get_balances()
                usdc_balance = self._get_usdc_balance(balances)
                
                if usdc_balance is None:
                    logger.warning(f"[风险控制] {exchange_name}: 未找到USDC余额")
                    continue
                
                # 检查余额不足平仓阈值
                if usdc_balance < self.config.balance_management.min_balance_close_position:
                    critical_balance_exchanges.add(exchange_name)
                    logger.error(
                        f"🚨 [风险控制] {exchange_name}: 余额严重不足 "
                        f"({usdc_balance} < {self.config.balance_management.min_balance_close_position})"
                    )
                
                # 检查余额不足警告阈值
                elif usdc_balance < self.config.balance_management.min_balance_warning:
                    low_balance_exchanges.add(exchange_name)
                    logger.warning(
                        f"⚠️  [风险控制] {exchange_name}: 余额不足 "
                        f"({usdc_balance} < {self.config.balance_management.min_balance_warning})"
                    )
                
            except Exception as e:
                # 🔥 只写入日志文件，不输出到控制台（避免UI抖动）
                # 如果是因为未配置API密钥导致的错误，使用debug级别
                error_msg = str(e)
                if "未配置SignerClient" in error_msg or "未配置API" in error_msg or "无法获取" in error_msg:
                    logger.debug(f"[风险控制] 检查{exchange_name}余额失败: {e}")
                else:
                    logger.error(f"[风险控制] 检查{exchange_name}余额失败: {e}", exc_info=True)
        
        # 更新风险状态
        self.risk_status.low_balance_exchanges = low_balance_exchanges
        self.risk_status.critical_balance_exchanges = critical_balance_exchanges
        
        # 处理余额不足
        if critical_balance_exchanges:
            await self._handle_critical_balance(critical_balance_exchanges)
        elif low_balance_exchanges:
            await self._handle_low_balance(low_balance_exchanges)
        else:
            # 余额恢复正常
            if self.risk_status.low_balance_exchanges or self.risk_status.critical_balance_exchanges:
                await self._handle_balance_recovered()
    
    def _get_usdc_balance(self, balances: List[BalanceData]) -> Optional[Decimal]:
        """
        从余额列表中获取USDC余额
        
        🔥 重要：必须使用总余额（total），而不是可用余额（free）
        原因：
        - Backpack统一账户中，资金可能在借出（lend）或订单冻结中
        - 可用余额（free）可能为0，但总余额（total）不为0
        - 使用总余额才能正确判断账户是否有资金进行交易
        """
        for balance in balances:
            currency = (balance.currency or '').upper()
            if not currency:
                continue
            
            # 🔥 兼容 USDC 及其变体（如 USDC.E），以及部分交易所使用的 USD 标识
            if currency == 'USDC' or currency.startswith('USDC') or currency == 'USD':
                total = balance.total
                if total is not None:
                    return total
                
                # 某些交易所可能只提供free/used字段
                free = balance.free or Decimal('0')
                used = balance.used or Decimal('0')
                return free + used
        return None
    
    async def _handle_critical_balance(self, exchanges: Set[str]):
        """处理余额严重不足"""
        if not self.risk_status.is_paused:
            self.risk_status.is_paused = True
            self.risk_status.pause_reason = f"余额严重不足: {', '.join(exchanges)}"
            
            logger.error(f"🚨 [风险控制] 暂停套利: {self.risk_status.pause_reason}")
            
            # 触发平仓所有仓位回调
            if self.on_close_all_positions:
                self.on_close_all_positions()
            
            # 触发暂停回调
            if self.on_pause:
                self.on_pause(self.risk_status.pause_reason)
    
    async def _handle_low_balance(self, exchanges: Set[str]):
        """处理余额不足"""
        if not self.risk_status.is_paused:
            self.risk_status.is_paused = True
            self.risk_status.pause_reason = f"余额不足: {', '.join(exchanges)}"
            
            logger.warning(f"⚠️  [风险控制] 暂停套利: {self.risk_status.pause_reason}")
            
            # 触发暂停回调
            if self.on_pause:
                self.on_pause(self.risk_status.pause_reason)
    
    async def _handle_balance_recovered(self):
        """处理余额恢复"""
        if self.risk_status.is_paused and "余额" in (self.risk_status.pause_reason or ""):
            self.risk_status.is_paused = False
            self.risk_status.pause_reason = None
            
            logger.info("✅ [风险控制] 余额已恢复，恢复套利操作")
            
            # 触发恢复回调
            if self.on_resume:
                self.on_resume()
    
    # ============================================================================
    # 持仓时间限制
    # ============================================================================
    
    async def _position_duration_monitor_loop(self):
        """持仓时间监控循环"""
        while self.running:
            try:
                await asyncio.sleep(60)  # 每分钟检查一次
                
                if not self.running:
                    break
                
                await self._check_position_duration()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[风险控制] 持仓时间监控错误: {e}", exc_info=True)
    
    async def _check_position_duration(self, positions_info: Optional[Dict[str, datetime]] = None):
        """
        检查持仓时间
        
        Args:
            positions_info: {symbol: open_time}，如果为None则从持仓信息中获取
        """
        if not self.config.position_duration.enabled:
            return
        
        if positions_info is None:
            # 如果没有提供持仓信息，跳过检查（需要外部提供）
            return
        
        current_time = datetime.now()
        max_duration_hours = self.config.position_duration.max_position_duration
        
        for symbol, open_time in positions_info.items():
            duration_hours = (current_time - open_time).total_seconds() / 3600
            
            if duration_hours > max_duration_hours:
                logger.warning(
                    f"⚠️  [风险控制] {symbol}: 持仓时间过长 "
                    f"({duration_hours:.1f}小时 > {max_duration_hours}小时)"
                )
                
                if self.config.position_duration.auto_close_on_timeout:
                    # 触发平仓回调（需要外部实现）
                    logger.warning(f"🛑 [风险控制] {symbol}: 自动平仓（持仓时间过长）")
    
    # ============================================================================
    # 每日交易次数限制
    # ============================================================================
    
    async def _daily_trade_limit_monitor_loop(self):
        """每日交易次数监控循环"""
        while self.running:
            try:
                await asyncio.sleep(3600)  # 每小时检查一次
                
                if not self.running:
                    break
                
                await self._reset_daily_trade_count_if_needed()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[风险控制] 每日交易次数监控错误: {e}", exc_info=True)
    
    async def _reset_daily_trade_count_if_needed(self):
        """如果需要，重置每日交易次数"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        if self.last_trade_date != today:
            self.daily_trade_count.clear()
            self.last_trade_date = today
            logger.debug(f"[风险控制] 重置每日交易次数: {today}")
    
    def check_daily_trade_limit(self) -> tuple[bool, Optional[str]]:
        """
        检查每日交易次数限制
        
        Returns:
            (是否允许, 拒绝原因)
        """
        if not self.config.daily_trade_limit.enabled:
            return True, None
        
        today = datetime.now().strftime('%Y-%m-%d')
        current_count = self.daily_trade_count.get(today, 0)
        
        if current_count >= self.config.daily_trade_limit.max_daily_trades:
            return False, f"每日交易次数已达上限: {current_count} >= {self.config.daily_trade_limit.max_daily_trades}"
        
        return True, None
    
    def record_trade(self):
        """记录交易"""
        today = datetime.now().strftime('%Y-%m-%d')
        self.daily_trade_count[today] = self.daily_trade_count.get(today, 0) + 1
    
    # ============================================================================
    # 交易对风险限制
    # ============================================================================
    
    def is_symbol_disabled(self, symbol: str) -> bool:
        """检查交易对是否被禁用"""
        return symbol in self.config.symbol_risk.disabled_symbols
    
    def is_symbol_high_risk(self, symbol: str) -> bool:
        """检查交易对是否为高风险"""
        return symbol in self.config.symbol_risk.high_risk_symbols
    
    # ============================================================================
    # 网络故障处理
    # ============================================================================
    
    def mark_network_failure(self, reason: str):
        """标记网络故障"""
        if not self.risk_status.network_failure:
            self.risk_status.network_failure = True
            self.risk_status.is_paused = True
            self.risk_status.pause_reason = f"网络故障: {reason}"
            
            logger.error(f"🚨 [风险控制] 网络故障: {reason}")
            
            if self.on_pause:
                self.on_pause(self.risk_status.pause_reason)
    
    def mark_network_recovered(self):
        """标记网络恢复"""
        if self.risk_status.network_failure:
            self.risk_status.network_failure = False
            
            # 如果只有网络故障导致暂停，则恢复
            if self.risk_status.pause_reason and "网络故障" in self.risk_status.pause_reason:
                self.risk_status.is_paused = False
                self.risk_status.pause_reason = None
                
                logger.info("✅ [风险控制] 网络已恢复，恢复套利操作")
                
                if self.on_resume:
                    self.on_resume()
    
    # ============================================================================
    # 交易所维护检测
    # ============================================================================
    
    def mark_exchange_maintenance(self, exchange: str):
        """标记交易所维护"""
        self.risk_status.exchange_maintenance.add(exchange)
        self.risk_status.is_paused = True
        self.risk_status.pause_reason = f"交易所维护: {exchange}"
        
        logger.warning(f"⚠️  [风险控制] 交易所维护: {exchange}")
        
        if self.on_pause:
            self.on_pause(self.risk_status.pause_reason)
    
    def mark_exchange_recovered(self, exchange: str):
        """标记交易所恢复"""
        self.risk_status.exchange_maintenance.discard(exchange)
        
        # 如果没有其他维护中的交易所，则恢复
        if not self.risk_status.exchange_maintenance:
            if self.risk_status.pause_reason and "交易所维护" in self.risk_status.pause_reason:
                self.risk_status.is_paused = False
                self.risk_status.pause_reason = None
                
                logger.info("✅ [风险控制] 所有交易所已恢复，恢复套利操作")
                
                if self.on_resume:
                    self.on_resume()
    
    # ============================================================================
    # 状态查询
    # ============================================================================
    
    def is_paused(self) -> bool:
        """检查是否暂停"""
        return self.risk_status.is_paused
    
    def get_pause_reason(self) -> Optional[str]:
        """获取暂停原因"""
        return self.risk_status.pause_reason
    
    def get_risk_status(self) -> RiskStatus:
        """获取风险状态"""
        return self.risk_status

