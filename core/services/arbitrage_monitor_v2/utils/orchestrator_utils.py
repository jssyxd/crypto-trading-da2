from __future__ import annotations

"""
Orchestrator 工具集
-------------------
抽离统一调度器中可复用的通用工具：
- `ThrottledLogger`: 针对重复日志的 key+message 节流
- `LiquidityFailureLogger`: 针对流动性不足日志的细粒度节流

使命是让 `UnifiedOrchestrator` 保持核心逻辑，降低噪音，行为与原实现完全一致。
"""

import time
from typing import Dict, Tuple, Optional, Any
import logging


class ThrottledLogger:
    """通用节流日志器，封装 key+message 频率控制。"""

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._records: Dict[str, Tuple[str, float]] = {}

    def log(
        self,
        key: str,
        message: str,
        *,
        level: str = "info",
        throttle_seconds: float = 0.5,
    ) -> None:
        now = time.time()
        last_entry = self._records.get(key)
        if last_entry:
            last_message, last_time = last_entry
            if last_message == message and now - last_time < throttle_seconds:
                return
        self._records[key] = (message, now)
        log_fn = getattr(self._logger, level, self._logger.info)
        log_fn(message)


class LiquidityFailureLogger:
    """对手盘不足日志节流器，避免重复输出。"""

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._records: Dict[str, Tuple[str, float]] = {}

    def log(
        self,
        symbol: str,
        *,
        reason: str,
        failure_detail: Optional[Dict[str, Any]],
        base_message: str,
        throttle_seconds: float = 3.0,
    ) -> None:
        key = f"{reason}:{symbol}"
        detail_signature = None
        extra = ""
        if failure_detail:
            extra = (
                f"{failure_detail['desc']} {failure_detail['exchange']}/"
                f"{failure_detail['market']}, 需求={failure_detail['quantity']}, "
                f"{failure_detail['detail']}"
            )
            detail_signature = (
                f"{failure_detail['desc']}:{failure_detail['exchange']}:"
                f"{failure_detail['market']}"
            )

        now = time.time()
        last_entry = self._records.get(key)
        if last_entry and detail_signature is not None:
            last_signature, last_time = last_entry
            if (
                last_signature == detail_signature
                and now - last_time < throttle_seconds
            ):
                return

        if detail_signature is not None:
            self._records[key] = (detail_signature, now)
        
        # 🔥 单行格式输出,更易读
        if extra:
            self._logger.warning("⚠️ [流动性不足] %s | %s", symbol, extra)
        else:
            self._logger.warning("%s", base_message)

    def clear(self, reason: str, symbol: str) -> None:
        self._records.pop(f"{reason}:{symbol}", None)

