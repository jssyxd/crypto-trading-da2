from __future__ import annotations

"""
Risk Control Utilities
----------------------
封装 `UnifiedOrchestrator` 中与风险控制相关的纯函数：
- 价格稳定性缓存与判定
- 对手盘流动性校验与日志节流
- 双限价避让 backoff 记录
保持原逻辑及日志输出，便于在其他模块复用。
"""

import logging
import time
from collections import deque
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.unified_orchestrator import UnifiedOrchestrator
from core.adapters.exchanges.utils.setup_logging import LoggingConfig

logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file="unified_orchestrator.log",
    console_formatter=None,
    file_formatter="detailed",
    level=logging.INFO,
)
logger.propagate = False


class RiskControlUtils:
    def __init__(self, orchestrator: "UnifiedOrchestrator") -> None:
        self.orc = orchestrator
        self._orderbook_liquidity_epsilon = Decimal("0.00000001")
        self._price_history: Dict[str, List[Tuple[float, Decimal, Decimal]]] = {}
        self._price_history_retention: Dict[str, float] = {}
        self._price_stability_state: Dict[str, str] = {}
        self._price_stability_log_times: Dict[str, float] = {}
        # 预编译价格稳定配置，运行时直接读取
        self._price_stability_settings_cache: Dict[str, Tuple[float, Optional[float]]] = {}
        self._liquidity_state: Dict[str, bool] = {}
        self._liquidity_log_times: Dict[str, float] = {}
        self._liquidity_symbol_log_times: Dict[str, float] = {}
        self._dual_limit_retry_state: Dict[str, Dict[str, float]] = {}
        # 统一节流配置，集中管理高频告警的打印频率
        self._throttle_cfg = {
            "price_stability_collecting": 30.0,  # 收集窗口状态打印间隔（从5秒增加到30秒）
            "price_stability_volatile": 60.0,    # 波动告警打印间隔（从15秒增加到60秒）
            # 流动性告警：不足状态 20s/次，恢复 40s/次，避免刷屏
            "liquidity_insufficient": 20.0,
            "liquidity_ok": 40.0,
            # 流动性总体节流（按 symbol+action 聚合），避免多腿同时刷屏
            "liquidity_symbol_aggregate": 15.0,
        }
        self._prime_price_stability_cache()

    # 获取价格稳定配置（带缓存）
    def _get_price_stability_settings(self, symbol: str) -> Tuple[float, Optional[float]]:
        return self._get_price_stability_settings_cached(symbol)

    # Price stability -----------------------------------------------------

    def record_price_sample(self, symbol: str, spread_data) -> None:
        history = self._price_history.setdefault(symbol, deque())
        timestamp = time.time()
        history.append((timestamp, spread_data.price_buy, spread_data.price_sell))

        window, _ = self._get_price_stability_settings(symbol)
        if window <= 0:
            while len(history) > 60:
                history.popleft()
            return

        retention = self._price_history_retention.get(symbol)
        target_retention = max(
            window + self.orc.data_freshness_seconds * 2, window * 4, 12.0
        )
        if retention is None or retention < target_retention:
            retention = target_retention
            self._price_history_retention[symbol] = retention

        cutoff = timestamp - retention
        while history and history[0][0] < cutoff:
            history.popleft()

    def reset_price_history(self, symbol: str, spread_data) -> None:
        history = self._price_history.setdefault(symbol, deque())
        history.clear()
        history.append((time.time(), spread_data.price_buy, spread_data.price_sell))

    def passes_price_stability(
        self,
        symbol: str,
        spread_data,
        *,
        action: str,
    ) -> bool:
        window, threshold = self._get_price_stability_settings(symbol)
        if window <= 0 or threshold is None or threshold <= 0:
            self._price_stability_state.pop(f"{symbol}:{action}", None)
            return True

        now = time.time()
        history = self._price_history.get(symbol)
        if not history:
            self._log_price_stability_state(
                symbol,
                action,
                "collecting",
                f"⏳ [价格稳定] {symbol} {action}: 正在收集{window:.1f}s窗口的数据",
                throttle_seconds=self._throttle_cfg["price_stability_collecting"],
            )
            return False

        coverage = now - history[0][0]
        if coverage < window:
            self._log_price_stability_state(
                symbol,
                action,
                "collecting",
                f"⏳ [价格稳定] {symbol} {action}: 窗口观察 {coverage:.2f}s/{window:.2f}s",
                throttle_seconds=self._throttle_cfg["price_stability_collecting"],
            )
            return False

        cutoff = now - window
        relevant = [entry for entry in history if entry[0] >= cutoff] or history[-1:]

        buy_values = [entry[1] for entry in relevant if entry[1] is not None]
        sell_values = [entry[2] for entry in relevant if entry[2] is not None]
        volatility_buy = self._calculate_volatility_percent(buy_values)
        volatility_sell = self._calculate_volatility_percent(sell_values)
        threshold_decimal = Decimal(str(threshold))

        if (
            volatility_buy > threshold_decimal
            or volatility_sell > threshold_decimal
        ):
            self._log_price_stability_state(
                symbol,
                action,
                "volatile",
                (
                    f"⚠️ [价格稳定] {symbol} {action}: 盘口波动 "
                    f"买{float(volatility_buy):.4f}%/卖{float(volatility_sell):.4f}% "
                    f"> 阈值 {threshold:.4f}%，重新计时"
                ),
                level="warning",
                throttle_seconds=self._throttle_cfg["price_stability_volatile"],
            )
            self.reset_price_history(symbol, spread_data)
            return False

        prev_state = self._price_stability_state.get(f"{symbol}:{action}")
        self._price_stability_state[f"{symbol}:{action}"] = "ok"
        if prev_state != "ok":
            logger.info(
                "✅ [价格稳定] %s %s: 波动买%.4f%%/卖%.4f%% ≤ 阈值 %.4f%% (窗口 %.1fs)",
                symbol,
                action,
                float(volatility_buy),
                float(volatility_sell),
                threshold,
                window,
            )
        return True

    # Liquidity -----------------------------------------------------------

    def verify_orderbook_liquidity(
        self,
        symbol: str,
        legs: List[Dict[str, Any]],
        action: str = "开仓",
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        for leg in legs:
            log_key = (
                f"{action}:{symbol}:{leg['exchange']}:{leg['symbol']}:{leg['desc']}"
            )
            ok, detail, skipped = self._check_orderbook_liquidity_for_leg(
                leg["exchange"],
                leg["symbol"],
                leg["quantity"],
                is_buy=leg["is_buy"],
                min_required=leg.get("min_quantity"),
            )
            if skipped:
                self._liquidity_state.pop(log_key, None)
                continue

            if not ok:
                if self._should_log_liquidity_event(log_key, False):
                    logger.warning(
                        "⚠️ [流动性校验] %s %s: %s 对手盘不足 (%s)，需求=%s",
                        action,
                        symbol,
                        leg["desc"],
                        detail,
                        leg["quantity"],  # 注：detail 内会包含 required/min_required（若配置了 min_orderbook_quantity）
                    )
                return False, {
                    "action": action,
                    "symbol": symbol,
                    "desc": leg["desc"],
                    "exchange": leg["exchange"],
                    "market": leg["symbol"],
                    "quantity": leg["quantity"],
                    "detail": detail,
                }

            if self._should_log_liquidity_event(log_key, True):
                logger.info(
                    "✅ [流动性校验] %s %s: %s %s/%s 需求=%s, %s",
                    action,
                    symbol,
                    leg["desc"],
                    leg["exchange"],
                    leg["symbol"],
                    leg["quantity"],
                    detail,
                )
        return True, None

    # Dual limit backoff --------------------------------------------------

    def should_skip_due_to_dual_limit_backoff(self, symbol: str) -> bool:
        state = self._dual_limit_retry_state.get(symbol)
        if not state:
            return False
        now = time.time()
        next_time = state.get("next_time", 0.0)
        if now >= next_time:
            self._dual_limit_retry_state.pop(symbol, None)
            return False
        remaining = max(0.0, next_time - now)
        current_delay = state.get("current_delay", remaining)
        logger.info(
            "⏸️ [V2开仓] %s: 双限价近期全部未成交，避让 %.1f 秒后再试（当前间隔 %.0f 秒）",
            symbol,
            remaining,
            current_delay,
        )
        return True

    def schedule_dual_limit_backoff(self, symbol: str) -> None:
        exec_cfg = getattr(self.orc.executor, "config", None)
        order_cfg = getattr(exec_cfg, "order_execution", None)
        if not order_cfg:
            return
        initial = max(
            1.0, float(getattr(order_cfg, "dual_limit_retry_initial_delay", 30))
        )
        max_delay = max(
            initial, float(getattr(order_cfg, "dual_limit_retry_max_delay", 600))
        )
        backoff = max(
            1.0, float(getattr(order_cfg, "dual_limit_retry_backoff_factor", 2.0))
        )
        state = self._dual_limit_retry_state.get(symbol)
        previous_delay = state["current_delay"] if state else 0.0
        next_delay = initial if previous_delay <= 0 else min(previous_delay * backoff, max_delay)
        next_time = time.time() + next_delay
        self._dual_limit_retry_state[symbol] = {
            "current_delay": next_delay,
            "next_time": next_time,
        }
        logger.warning(
            "⚠️ [V2开仓] %s: 双限价未能成交，%d 秒后再次尝试 (当前避让 %.0f 秒)",
            symbol,
            int(next_delay),
            next_delay,
        )

    def clear_dual_limit_backoff(self, symbol: str) -> None:
        self._dual_limit_retry_state.pop(symbol, None)

    # Internal helpers ----------------------------------------------------

    def _prime_price_stability_cache(self) -> None:
        """启动时预编译价格稳定配置，减少循环内查配置的开销。"""
        symbols = getattr(self.orc.monitor_config, "symbols", []) or []
        for sym in symbols:
            window, threshold = self._read_price_stability_settings(sym)
            self._price_stability_settings_cache[sym] = (window, threshold)

    def _get_price_stability_settings_cached(self, symbol: str) -> Tuple[float, Optional[float]]:
        if symbol in self._price_stability_settings_cache:
            return self._price_stability_settings_cache[symbol]
        window, threshold = self._read_price_stability_settings(symbol)
        self._price_stability_settings_cache[symbol] = (window, threshold)
        return window, threshold

    def _read_price_stability_settings(self, symbol: str) -> Tuple[float, Optional[float]]:
        try:
            config = self.orc.config_manager.get_config(symbol)
            grid_cfg = config.grid_config
            window = float(grid_cfg.price_stability_window_seconds or 0.0)
            threshold = grid_cfg.price_stability_threshold_pct
            return window, (float(threshold) if threshold is not None else None)
        except Exception:
            return 0.0, None

    def _log_price_stability_state(
        self,
        symbol: str,
        action: str,
        state: str,
        message: Optional[str],
        level: str = "info",
        throttle_seconds: float = 5.0,
    ) -> None:
        state_key = f"{symbol}:{action}"
        log_key = f"{symbol}:{action}:{state}"
        now = time.time()
        state_changed = self._price_stability_state.get(state_key) != state
        last_logged = self._price_stability_log_times.get(log_key, 0.0)
        
        # 🔥 修复：即使状态改变，也要遵守最小节流时间（减少刷屏）
        # 只有在首次打印时才允许立即打印（last_logged == 0.0）
        time_passed = now - last_logged
        should_log = (last_logged == 0.0 and state_changed) or (time_passed >= throttle_seconds)
        
        if not should_log:
            return
        self._price_stability_state[state_key] = state
        self._price_stability_log_times[log_key] = now
        if message:
            log_fn = getattr(logger, level, logger.info)
            log_fn(message)

    @staticmethod
    def _calculate_volatility_percent(values: List[Decimal]) -> Decimal:
        if not values:
            return Decimal("0")
        max_price = max(values)
        min_price = min(values)
        if min_price <= Decimal("0"):
            return Decimal("0")
        return (max_price - min_price) / min_price * Decimal("100")

    def _should_log_liquidity_event(self, key: str, state: bool) -> bool:
        """
        流动性日志节流：
        - 状态变化时立即打印
        - 同一状态下，至少间隔 N 秒再打印
        - 额外按 symbol+action 聚合节流，防止多腿同时刷屏
        """
        previous = self._liquidity_state.get(key)
        now = time.time()
        last_time = self._liquidity_log_times.get(key, 0.0)

        # 选择节流间隔（单腿维度）
        interval = (
            self._throttle_cfg["liquidity_insufficient"]
            if not state
            else self._throttle_cfg["liquidity_ok"]
        )

        state_changed = previous is None or previous != state
        time_passed = now - last_time
        per_leg_ok = state_changed or time_passed >= interval

        if not per_leg_ok:
            return False

        # 额外按 symbol+action 聚合节流
        # key 格式: action:symbol:exchange:market:desc
        symbol_action = ":".join(key.split(":")[:2]) if ":" in key else key
        agg_interval = self._throttle_cfg["liquidity_symbol_aggregate"]
        agg_last = self._liquidity_symbol_log_times.get(symbol_action, 0.0)
        if now - agg_last < agg_interval:
            return False

        # 通过节流，记录时间戳
        self._liquidity_state[key] = state
        self._liquidity_log_times[key] = now
        self._liquidity_symbol_log_times[symbol_action] = now
        return True

    def _check_orderbook_liquidity_for_leg(
        self,
        exchange: str,
        symbol: str,
        quantity: Decimal,
        *,
        is_buy: bool,
        min_required: Optional[Decimal] = None,
    ) -> Tuple[bool, str, bool]:
        orderbook = self.orc.data_processor.get_orderbook(
            exchange,
            symbol,
            max_age_seconds=self.orc.data_freshness_seconds,
        )
        if not orderbook:
            return False, "订单簿缺失或已过期", False

        level = orderbook.best_ask if is_buy else orderbook.best_bid
        if not level:
            side = "卖" if is_buy else "买"
            return False, f"{side}盘无深度", False

        size = getattr(level, "size", None)
        if size is None:
            return True, "盘口未提供数量，已跳过校验", True

        available = Decimal(str(size))
        if available <= Decimal("0"):
            return False, f"对手盘数量<=0，可用={available}", False

        price = getattr(level, "price", None)
        price_display = ""
        if price is not None:
            try:
                price_display = f" @ {Decimal(str(price))}"
            except Exception:
                price_display = ""

        required = quantity
        if min_required is not None:
            required = max(required, min_required)

        min_required_display = ""
        required_display = ""
        try:
            if min_required is not None:
                min_required_display = f", min_required={min_required}"
            required_display = f", required={required}"
        except Exception:
            # 仅用于日志显示，不影响业务流程
            min_required_display = ""
            required_display = ""

        if available + self._orderbook_liquidity_epsilon < required:
            return (
                False,
                f"对手盘数量不足，可用={available}{price_display}{required_display}{min_required_display}",
                False,
            )

        return (
            True,
            f"可用数量={available}{price_display}{required_display}{min_required_display}",
            False,
        )


__all__ = ["RiskControlUtils"]

