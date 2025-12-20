"""
多交易所套利配置管理

用于定义 1 对多 / 多对多 跨所套利的交易所组合。该配置只负责
生成组合列表（trading_pair_id + 交易所 + Symbol），具体决策逻辑
在 UnifiedOrchestrator 中实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
import logging

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


logger = logging.getLogger(__name__)


@dataclass
class TradingPair:
    """多交易所套利组合"""

    trading_pair_id: str
    exchange_a: str
    exchange_b: str
    symbol: str
    allow_reverse: bool = True
    min_spread_pct: Optional[float] = None

    def normalized_exchange_a(self) -> str:
        return self.exchange_a.lower().strip()

    def normalized_exchange_b(self) -> str:
        return self.exchange_b.lower().strip()

    def normalized_symbol(self) -> str:
        return self.symbol.upper().strip()

    def base_symbol(self) -> str:
        """提取 Symbol 的简写（如 BTC-USDC-PERP -> BTC）"""
        return TradingPair.extract_symbol_short(self.symbol)

    @staticmethod
    def extract_symbol_short(symbol: str) -> str:
        if not symbol:
            return ""
        return symbol.split('-')[0].upper().strip()


class MultiExchangeArbitrageConfigManager:
    """多交易所套利配置管理器"""

    def __init__(self, config_path: Optional[Path] = None):
        default_path = Path("config/arbitrage/multi_exchange_arbitrage.yaml")
        self.config_path = Path(config_path) if config_path else default_path
        self._pairs: List[TradingPair] = []
        self._load()

    def _load(self) -> None:
        if not self.config_path.exists():
            logger.info("🔍 未找到多交易所套利配置: %s", self.config_path)
            return

        if yaml is None:
            logger.warning("⚠️ 未安装 PyYAML，无法读取多交易所套利配置: %s", self.config_path)
            return

        try:
            with self.config_path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as exc:  # pragma: no cover
            logger.warning("⚠️ 加载多交易所套利配置失败 %s: %s", self.config_path, exc)
            return

        if not raw or not raw.get("enabled"):
            logger.info("ℹ️ 多交易所套利配置未启用: %s", self.config_path)
            return

        mode = (raw.get("mode") or "one_to_many").lower()
        allow_reverse = bool(raw.get("allow_reverse", True))
        min_spread_pct = raw.get("min_spread_pct")

        if mode == "one_to_many":
            self._pairs = self._generate_one_to_many(
                center_exchange=raw.get("center_exchange"),
                counter_exchanges=raw.get("counter_exchanges") or [],
                symbols=raw.get("symbols") or [],
                allow_reverse=allow_reverse,
                min_spread_pct=min_spread_pct,
            )
        elif mode == "many_to_many":
            self._pairs = self._generate_many_to_many(
                exchanges=raw.get("exchanges") or [],
                symbols=raw.get("symbols") or [],
                allow_reverse=allow_reverse,
                min_spread_pct=min_spread_pct,
            )
        else:
            logger.warning("⚠️ 未知的多交易所套利模式: %s", mode)
            return

        if self._pairs:
            logger.info("✅ 加载多交易所套利配置 %d 条 (%s)", len(self._pairs), self.config_path)

    def _generate_one_to_many(
        self,
        center_exchange: Optional[str],
        counter_exchanges: List[str],
        symbols: List[str],
        allow_reverse: bool,
        min_spread_pct: Optional[float],
    ) -> List[TradingPair]:
        if not center_exchange or not counter_exchanges or not symbols:
            logger.warning("⚠️ one_to_many 配置缺少 center_exchange/counter_exchanges/symbols，已忽略")
            return []

        pairs: List[TradingPair] = []
        for counter in counter_exchanges:
            for symbol in symbols:
                pair = self._build_pair(
                    exchange_a=center_exchange,
                    exchange_b=counter,
                    symbol=symbol,
                    allow_reverse=allow_reverse,
                    min_spread_pct=min_spread_pct,
                )
                if pair:
                    pairs.append(pair)
        return pairs

    def _generate_many_to_many(
        self,
        exchanges: List[str],
        symbols: List[str],
        allow_reverse: bool,
        min_spread_pct: Optional[float],
    ) -> List[TradingPair]:
        if not exchanges or len(exchanges) < 2 or not symbols:
            logger.warning("⚠️ many_to_many 配置缺少 exchanges/symbols，已忽略")
            return []

        pairs: List[TradingPair] = []
        normalized = [ex for ex in exchanges if isinstance(ex, str)]
        for i in range(len(normalized)):
            for j in range(i + 1, len(normalized)):
                ex_a = normalized[i]
                ex_b = normalized[j]
                for symbol in symbols:
                    pair = self._build_pair(
                        exchange_a=ex_a,
                        exchange_b=ex_b,
                        symbol=symbol,
                        allow_reverse=allow_reverse,
                        min_spread_pct=min_spread_pct,
                    )
                    if pair:
                        pairs.append(pair)
        return pairs

    def _build_pair(
        self,
        exchange_a: str,
        exchange_b: str,
        symbol: str,
        allow_reverse: bool,
        min_spread_pct: Optional[float],
    ) -> Optional[TradingPair]:
        if not exchange_a or not exchange_b or not symbol:
            return None

        ex_a = exchange_a.lower().strip()
        ex_b = exchange_b.lower().strip()
        exchanges = sorted([ex_a, ex_b])
        symbol_normalized = symbol.upper().strip()
        symbol_short = TradingPair.extract_symbol_short(symbol_normalized)

        trading_pair_id = f"{exchanges[0].upper()}_{exchanges[1].upper()}_{symbol_short}"
        return TradingPair(
            trading_pair_id=trading_pair_id,
            exchange_a=exchanges[0],
            exchange_b=exchanges[1],
            symbol=symbol_normalized,
            allow_reverse=allow_reverse,
            min_spread_pct=min_spread_pct,
        )

    def get_pairs(self) -> List[TradingPair]:
        return list(self._pairs)

    def get_required_symbols(self) -> List[str]:
        """返回所有组合涉及的底层symbol，用于行情订阅"""
        symbols = [pair.normalized_symbol() for pair in self._pairs]
        seen = set()
        ordered: List[str] = []
        for sym in symbols:
            if sym not in seen:
                seen.add(sym)
                ordered.append(sym)
        return ordered

