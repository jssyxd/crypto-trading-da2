from __future__ import annotations

"""
Spread Pipeline
---------------
负责 orchestrator 中“获取行情 → 计算价差 → 触发开/平仓”这条纯逻辑流水线。
目标：
- 让 `UnifiedOrchestrator` 主类只保留调度入口，减少重复代码
- 保持日志、指标、顺序与原实现完全一致
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from core.adapters.exchanges.utils.setup_logging import LoggingConfig
from ..analysis.spread_calculator import SpreadData
from ..models import FundingRateData, SegmentedPosition

if TYPE_CHECKING:
    from .unified_orchestrator import UnifiedOrchestrator
    from core.adapters.exchanges.models import OrderBookData
    from ..config.multi_exchange_config import TradingPair


logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file="unified_orchestrator.log",
    console_formatter=None,
    file_formatter="detailed",
    level=logging.WARNING,  # 高频行情/持仓细节保持 WARNING，避免刷屏
)
logger.propagate = False


class SpreadPipeline:
    def __init__(self, orchestrator: "UnifiedOrchestrator") -> None:
        self.orchestrator = orchestrator
        self._throttle_times: Dict[str, float] = {}

    async def process_symbol(self, symbol: str) -> None:
        """
        ⚠️ 已废弃的方法 - 不推荐使用
        
        此方法使用 symbol 级别的持仓管理，但当前系统使用 trading_pair_id（包含交易所对）
        作为持仓 key，导致架构不匹配：
        
        问题：
        1. 无法正确检测持仓状态（key 不匹配）
        2. 无法处理同一 symbol 的多个交易所对并存场景
        3. 持仓记忆功能无法正常工作
        
        推荐做法：
        - 将 symbol 添加到 config/arbitrage/multi_exchange_arbitrage.yaml
        - 系统会自动使用 _process_single_trading_pair 处理（支持多交易所对并存）
        
        当前配置下，所有 symbols 都在 multi_exchange_arbitrage.yaml 中，
        此方法永远不会被调用（被 orchestrator 的 814 行过滤）。
        """
        orc = self.orchestrator
        logger.error(
            f"❌ [{symbol}] 尝试使用已废弃的 process_symbol 方法！\n"
            f"   此方法在当前架构下无法正确工作（symbol级持仓 vs trading_pair_id级持仓）。\n"
            f"   请将 {symbol} 添加到 config/arbitrage/multi_exchange_arbitrage.yaml，\n"
            f"   系统将自动使用正确的 _process_single_trading_pair 方法处理。"
        )
        return
        
        # ========== 以下为原有代码（已废弃，仅保留作为参考）==========
        try:
            symbol_upper = symbol.upper()
            orderbooks = await self._get_orderbooks(symbol_upper)
            if len(orderbooks) < 2:
                return

            spreads = orc.spread_calculator.calculate_spreads_multi_exchange_directions(
                symbol_upper,
                orderbooks,
            )
            if not spreads:
                return

            position = orc.decision_engine.get_position(symbol_upper)
            opening_spread: Optional[SpreadData] = None
            closing_spread: Optional[SpreadData] = None
            positive_spreads: List[SpreadData] = []

            if position and position.total_quantity > Decimal("0"):
                memory_spreads = self._build_spreads_from_position(symbol_upper, position)
                if not memory_spreads:
                    logger.warning(
                        "⚠️ [价差] %s: 记忆方向缺少盘口数据，暂停本轮信号。",
                        symbol_upper,
                    )
                    return
                opening_spread, closing_spread = memory_spreads
                if opening_spread.spread_pct > 0:
                    positive_spreads = [opening_spread]
            else:
                positive_spreads = [s for s in spreads if s.spread_pct > 0]
                opening_spread = (
                    max(positive_spreads, key=lambda s: s.spread_pct)
                    if positive_spreads
                    else max(spreads, key=lambda s: s.spread_pct)
                )
                closing_spread = orc.spread_calculator.build_closing_spread_from_orderbooks(
                    opening_spread,
                    orderbooks,
                )

            # 🔥 只对最优价差记录价格样本和决策快照（用于历史数据和UI显示）
            orc._record_price_sample(symbol_upper, opening_spread)
            orc._capture_decision_snapshot(symbol_upper, opening_spread)

            funding_rate_data = self._get_funding_rate_data(
                symbol_upper,
                opening_spread.exchange_buy,
                opening_spread.exchange_sell,
            )

            if closing_spread:
                await orc._check_and_close(symbol_upper, closing_spread, funding_rate_data)
            else:
                logger.warning(
                    "⚠️ [价差] %s: 无法生成平仓视角价差（缺少真实盘口），跳过平仓判断",
                    symbol_upper,
                )
            
            # 🔥 一对多模式：遍历所有正价差的交易所组合，每个都独立检查开仓条件
            if positive_spreads:
                for spread in positive_spreads:
                    # 🔥 为每个价差记录样本（用于持续性检查）
                    orc._record_price_sample(symbol_upper, spread)
                    
                    funding_rate_data_for_pair = self._get_funding_rate_data(
                        symbol_upper,
                        spread.exchange_buy,
                        spread.exchange_sell,
                    )
                    await orc._check_and_open(symbol_upper, spread, funding_rate_data_for_pair)
            elif opening_spread.spread_pct > 0:
                # 兜底：如果没有正价差但最优价差>0，也处理
                await orc._check_and_open(symbol_upper, opening_spread, funding_rate_data)
        except Exception as exc:
            logger.error(f"❌ [统一调度] 处理{symbol}异常: {exc}", exc_info=True)

    async def process_multi_leg_pairs(self) -> None:
        """处理多腿套利组合。"""
        orc = self.orchestrator
        if not orc.multi_leg_pairs:
            return

        try:
            orderbooks: Dict[Tuple[str, str], "OrderBookData"] = {}

            for pair in orc.multi_leg_pairs:
                leg1_symbol = pair.leg_primary.normalized_symbol()
                leg1_exchange = pair.leg_primary.normalized_exchange()
                leg1_ob = orc.data_processor.get_orderbook(
                    leg1_exchange,
                    leg1_symbol,
                    max_age_seconds=orc.data_freshness_seconds,
                )
                if leg1_ob:
                    orderbooks[(leg1_exchange, leg1_symbol)] = leg1_ob

                leg2_symbol = pair.leg_secondary.normalized_symbol()
                leg2_exchange = pair.leg_secondary.normalized_exchange()
                leg2_ob = orc.data_processor.get_orderbook(
                    leg2_exchange,
                    leg2_symbol,
                    max_age_seconds=orc.data_freshness_seconds,
                )
                if leg2_ob:
                    orderbooks[(leg2_exchange, leg2_symbol)] = leg2_ob

            for pair in orc.multi_leg_pairs:
                spreads = orc.spread_calculator.calculate_multi_leg_spread(
                    pair_id=pair.pair_id,
                    leg_primary_exchange=pair.leg_primary.normalized_exchange(),
                    leg_primary_symbol=pair.leg_primary.normalized_symbol(),
                    leg_secondary_exchange=pair.leg_secondary.normalized_exchange(),
                    leg_secondary_symbol=pair.leg_secondary.normalized_symbol(),
                    orderbooks=orderbooks,
                    allow_reverse=pair.allow_reverse,
                )

                if not spreads:
                    continue

                best_spread = max(spreads, key=lambda s: s.spread_pct)
                closing_spread = orc.spread_calculator.calculate_multi_leg_closing_spread(
                    pair_id=pair.pair_id,
                    leg_primary_exchange=pair.leg_primary.normalized_exchange(),
                    leg_primary_symbol=pair.leg_primary.normalized_symbol(),
                    leg_secondary_exchange=pair.leg_secondary.normalized_exchange(),
                    leg_secondary_symbol=pair.leg_secondary.normalized_symbol(),
                    orderbooks=orderbooks,
                    opening_direction=best_spread,
                )

                if pair.min_spread_pct and best_spread.spread_pct < pair.min_spread_pct:
                    continue

                orc._record_price_sample(pair.pair_id, best_spread)
                orc._capture_decision_snapshot(pair.pair_id, best_spread)

                await orc._check_and_open(
                    symbol=pair.pair_id,
                    spread_data=best_spread,
                    funding_rate_data=None,
                    config_symbol=pair.pair_id,
                )

                if closing_spread:
                    await orc._check_and_close(
                        symbol=pair.pair_id,
                        spread_data=closing_spread,
                        funding_rate_data=None,
                        config_symbol=pair.pair_id,
                    )
                else:
                    logger.warning(
                        "⚠️ [价差] %s: 无法生成平仓视角价差（缺少真实盘口），跳过平仓判断",
                        pair.pair_id,
                    )

        except Exception as exc:
            logger.error(f"❌ [统一调度] 处理多腿套利异常: {exc}", exc_info=True)

    async def process_trading_pairs(self) -> None:
        """处理多交易所套利组合。"""
        orc = self.orchestrator
        if not orc.multi_exchange_pairs:
            return

        tasks = [
            self._process_single_trading_pair(pair) for pair in orc.multi_exchange_pairs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for pair, result in zip(orc.multi_exchange_pairs, results):
            if isinstance(result, Exception):
                logger.error(
                    "❌ [统一调度] 处理多交易所套利对 %s 异常: %s",
                    pair.trading_pair_id,
                    result,
                    exc_info=result,
                )

    async def _process_single_trading_pair(self, pair: "TradingPair") -> None:
        orc = self.orchestrator
        symbol_key = pair.trading_pair_id
        base_symbol = pair.normalized_symbol()
        exchange_a = pair.normalized_exchange_a()
        exchange_b = pair.normalized_exchange_b()

        orderbook_a = orc.data_processor.get_orderbook(
            exchange_a,
            base_symbol,
            max_age_seconds=orc.data_freshness_seconds,
        )
        orderbook_b = orc.data_processor.get_orderbook(
            exchange_b,
            base_symbol,
            max_age_seconds=orc.data_freshness_seconds,
        )
        if not orderbook_a or not orderbook_b:
            return

        orderbooks = {
            exchange_a: orderbook_a,
            exchange_b: orderbook_b,
        }

        position = orc.decision_engine.get_position(symbol_key)
        has_position = position and position.total_quantity > Decimal("0")

        if has_position:
            memory_spreads = self._build_spreads_from_position(symbol_key, position)  # type: ignore[arg-type]
            if not memory_spreads:
                logger.warning(
                    "⚠️ [价差] %s: 记忆方向缺少盘口数据，暂停本轮信号。",
                    symbol_key,
                )
                return
            opening_spread, closing_spread = memory_spreads
        else:
            spreads = orc.spread_calculator.calculate_spreads_multi_exchange_directions(
                base_symbol,
                orderbooks,
            )
            if not spreads:
                return

            filtered_spreads = spreads
            if not pair.allow_reverse:
                expected_buy = exchange_a
                expected_sell = exchange_b
                filtered_spreads = [
                    s
                    for s in spreads
                    if (s.exchange_buy or "").lower() == expected_buy.lower()
                    and (s.exchange_sell or "").lower() == expected_sell.lower()
                ]
                if not filtered_spreads:
                    return

            positive_spreads = [s for s in filtered_spreads if s.spread_pct > 0]
            opening_spread = (
                max(positive_spreads, key=lambda s: s.spread_pct)
                if positive_spreads
                else max(filtered_spreads, key=lambda s: s.spread_pct)
            )

            closing_spread = orc.spread_calculator.build_closing_spread_from_orderbooks(
                opening_spread,
                orderbooks,
            )
            if not closing_spread:
                logger.warning(
                    "⚠️ [价差] %s: 无法生成平仓视角价差（缺少真实盘口），跳过平仓判断",
                    symbol_key,
                )
                return

        orc._record_price_sample(symbol_key, opening_spread)
        orc._capture_decision_snapshot(symbol_key, opening_spread)

        if (
            not has_position
            and pair.min_spread_pct is not None
            and opening_spread.spread_pct < pair.min_spread_pct
        ):
            return

        funding_rate_data = self._get_funding_rate_data(
            base_symbol,
            opening_spread.exchange_buy,
            opening_spread.exchange_sell,
        )

        await orc._check_and_close(
            symbol_key,
            closing_spread,
            funding_rate_data,
            config_symbol=base_symbol,
        )
        # 🔥 有持仓时必须检查开仓（即使价差为负），因为可能触发反向平仓检测
        # 无持仓时只检查正价差
        if has_position or opening_spread.spread_pct > 0:
            await orc._check_and_open(
                symbol_key,
                opening_spread,
                funding_rate_data,
                config_symbol=base_symbol,
            )

    async def _get_orderbooks(
        self,
        symbol: str,
    ) -> Dict[str, "OrderBookData"]:
        """复用原 `_get_orderbooks_for_symbol` 逻辑。"""
        orc = self.orchestrator
        orderbooks: Dict[str, "OrderBookData"] = {}
        excluded: List[str] = []

        for exchange_name in orc.monitor_config.exchanges:
            orderbook = orc.data_processor.get_orderbook(
                exchange_name,
                symbol,
                max_age_seconds=orc.data_freshness_seconds,
            )
            if orderbook:
                orderbooks[exchange_name] = orderbook
            else:
                excluded.append(exchange_name)

        if excluded:
            logger.debug(
                "[数据过滤] %s 排除了 %d 个交易所的过期数据: %s",
                symbol,
                len(excluded),
                ", ".join(excluded),
            )

        if len(orderbooks) < 2:
            logger.debug(
                "[数据不足] %s 可用交易所不足 2 个（当前: %d）",
                symbol,
                len(orderbooks),
            )

        return orderbooks

    def _build_spreads_from_position(
        self,
        symbol: str,
        position: SegmentedPosition,
    ) -> Optional[Tuple[SpreadData, SpreadData]]:
        """根据持仓记忆重建开/平仓价差，确保方向固定。"""
        orc = self.orchestrator
        data_processor = orc.data_processor
        max_age = orc.data_freshness_seconds

        buy_exchange = position.exchange_buy
        sell_exchange = position.exchange_sell
        if not buy_exchange or not sell_exchange:
            logger.warning("⚠️ [价差] %s: 持仓缺少记忆的交易所信息。", symbol)
            return None

        buy_symbol = (position.buy_symbol or symbol).upper()
        sell_symbol = (position.sell_symbol or symbol).upper()

        buy_leg_ob = data_processor.get_orderbook(
            buy_exchange,
            buy_symbol,
            max_age_seconds=max_age,
        )
        sell_leg_ob = data_processor.get_orderbook(
            sell_exchange,
            sell_symbol,
            max_age_seconds=max_age,
        )

        if not buy_leg_ob or not sell_leg_ob:
            self._log_warning_throttled(
                key=f"{symbol}:memory_orderbook_missing",
                message=(
                    f"⚠️ [价差] {symbol}: 记忆方向缺少实时盘口（{buy_exchange}/{sell_exchange}）。"
                ),
            )
            return None

        buy_ask = buy_leg_ob.best_ask
        buy_bid = buy_leg_ob.best_bid
        sell_ask = sell_leg_ob.best_ask
        sell_bid = sell_leg_ob.best_bid
        if not all([buy_ask, buy_bid, sell_ask, sell_bid]):
            self._log_warning_throttled(
                key=f"{symbol}:memory_orderbook_incomplete",
                message=f"⚠️ [价差] {symbol}: 记忆方向盘口不完整，暂不执行。",
            )
            return None

        opening_spread = SpreadData(
            symbol=symbol,
            exchange_buy=buy_exchange,
            exchange_sell=sell_exchange,
            price_buy=buy_ask.price,
            price_sell=sell_bid.price,
            size_buy=buy_ask.size,
            size_sell=sell_bid.size,
            spread_abs=sell_bid.price - buy_ask.price,
            spread_pct=float(((sell_bid.price - buy_ask.price) / buy_ask.price) * 100),
            buy_symbol=buy_symbol,
            sell_symbol=sell_symbol,
        )

        # 🔥 关键修复：平仓时需要反向操作
        # 开仓记忆: buy_exchange买 + sell_exchange卖
        # 平仓应该: sell_exchange买 + buy_exchange卖
        # 但价格要用当前市场的对手盘价格：
        # - 在 sell_exchange 买 → 用 sell_exchange 的 ask (sell_ask)
        # - 在 buy_exchange 卖 → 用 buy_exchange 的 bid (buy_bid)
        closing_spread = SpreadData(
            symbol=symbol,
            exchange_buy=sell_exchange,       # 平仓时在原卖出交易所买入
            exchange_sell=buy_exchange,       # 平仓时在原买入交易所卖出
            price_buy=sell_ask.price,         # 用原卖出交易所的ask价格买入
            price_sell=buy_bid.price,         # 用原买入交易所的bid价格卖出
            size_buy=sell_ask.size,
            size_sell=buy_bid.size,
            spread_abs=buy_bid.price - sell_ask.price,
            spread_pct=float(((buy_bid.price - sell_ask.price) / sell_ask.price) * 100),
            buy_symbol=sell_symbol,
            sell_symbol=buy_symbol,
        )

        return opening_spread, closing_spread

    def _log_warning_throttled(
        self,
        *,
        key: str,
        message: str,
        interval_seconds: float = 60.0,
    ) -> None:
        """限制同一警告的打印频率。"""
        now = time.time()
        last = self._throttle_times.get(key, 0)
        if now - last >= interval_seconds:
            logger.warning(message)
            self._throttle_times[key] = now

    def _log_info_throttle(
        self,
        *,
        key: str,
        message: str,
        interval: float = 60.0,
    ) -> None:
        """限制同一Info日志的打印频率。"""
        now = time.time()
        last = self._throttle_times.get(key, 0)
        if now - last >= interval:
            logger.info(message)
            self._throttle_times[key] = now

    def _get_funding_rate_data(
        self,
        symbol: str,
        exchange_buy: str,
        exchange_sell: str,
    ) -> Optional[FundingRateData]:
        """从 orchestrator 复用资金费率获取实现。"""
        orc = self.orchestrator
        try:
            funding_rates = orc.data_processor.get_latest_funding_rates()
            if symbol not in funding_rates:
                return None

            symbol_funding = funding_rates[symbol]
            funding_buy = symbol_funding.get(exchange_buy, 0.0)
            funding_sell = symbol_funding.get(exchange_sell, 0.0)
            funding_diff = abs(funding_sell - funding_buy)
            is_favorable = funding_sell > funding_buy
            funding_diff_annual = funding_diff * 365 * 3

            return FundingRateData(
                exchange_buy=exchange_buy,
                exchange_sell=exchange_sell,
                funding_rate_buy=funding_buy,
                funding_rate_sell=funding_sell,
                funding_rate_diff=funding_diff,
                funding_rate_diff_annual=funding_diff_annual,
                is_favorable_for_position=is_favorable,
            )
        except Exception as exc:
            logger.debug(f"[统一调度] 获取资金费率失败: {symbol}: {exc}")
            return None

