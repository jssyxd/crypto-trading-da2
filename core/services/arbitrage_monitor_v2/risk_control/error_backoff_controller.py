"""
错误指数避让控制器

职责：
- 监控交易所错误（21104 invalid nonce, 429 Too Many Requests）
- 实现指数退避机制
- 暂停开仓和平仓操作
- 自动恢复
"""

import time
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

from core.adapters.exchanges.utils.setup_logging import LoggingConfig

logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file='error_backoff.log',
    console_formatter=None,
    file_formatter='detailed',
    level=logging.INFO
)
logger.propagate = False


class ErrorType(Enum):
    """错误类型"""
    INVALID_NONCE = "21104"  # Lighter nonce错误
    RATE_LIMIT = "429"       # 限流错误（通用429）
    RATE_LIMIT_L1 = "23000"  # L1地址限流（Too Many Requests）


@dataclass
class BackoffState:
    """避让状态"""
    exchange: str                    # 交易所
    error_type: ErrorType           # 错误类型
    error_count: int                # 连续错误次数
    last_error_time: datetime       # 最后错误时间
    pause_until: datetime           # 暂停到何时
    pause_duration_seconds: float   # 本次暂停时长（秒）
    recovery_logged: bool = False   # 是否已打印恢复日志（避免重复打印）


class ErrorBackoffController:
    """错误指数避让控制器"""
    
    # 指数退避配置
    MIN_BACKOFF_SECONDS = 120       # 最短暂停 2 分钟
    MAX_BACKOFF_SECONDS = 3600      # 最长暂停 1 小时
    BACKOFF_MULTIPLIER = 2.0        # 退避倍数
    ERROR_RESET_SECONDS = 1800      # 30分钟无错误后重置计数
    
    def __init__(self):
        """初始化控制器"""
        # 交易所避让状态 {exchange: BackoffState}
        self._backoff_states: Dict[str, BackoffState] = {}
        
        # 最后日志时间（避免刷屏）
        self._last_log_times: Dict[str, float] = {}

        # 重启钩子（局部重启适配器用） {exchange: callable}
        self._restart_hooks: Dict[str, callable] = {}
        # 重启节流，避免频繁重启
        self._last_restart_ts: Dict[str, float] = {}
        
        logger.info("✅ [错误避让] 指数避让控制器初始化完成")
    
    def register_error(
        self,
        exchange: str,
        error_code: str,
        error_message: str = ""
    ) -> None:
        """
        注册一个错误
        
        Args:
            exchange: 交易所名称
            error_code: 错误码（如 "21104", "429"）
            error_message: 错误信息
        """
        # 判断是否是需要避让的错误
        error_type = self._parse_error_type(error_code)
        if error_type is None:
            return
        
        exchange_lower = exchange.lower()
        now = datetime.now()
        
        # 获取或创建避让状态
        state = self._backoff_states.get(exchange_lower)
        
        if state is None:
            # 首次错误
            error_count = 1
            pause_duration = self.MIN_BACKOFF_SECONDS
        else:
            # 检查是否需要重置计数（距离上次错误太久）
            time_since_last_error = (now - state.last_error_time).total_seconds()
            if time_since_last_error > self.ERROR_RESET_SECONDS:
                # 重置计数
                error_count = 1
                pause_duration = self.MIN_BACKOFF_SECONDS
                logger.info(
                    f"🔄 [错误避让] {exchange}: 距离上次错误已过 "
                    f"{time_since_last_error / 60:.1f} 分钟，重置错误计数"
                )
            else:
                # 累加错误次数，计算新的暂停时长
                error_count = state.error_count + 1
                # 指数退避：2分钟 * 2^(n-1)，上限10分钟
                pause_duration = min(
                    self.MIN_BACKOFF_SECONDS * (self.BACKOFF_MULTIPLIER ** (error_count - 1)),
                    self.MAX_BACKOFF_SECONDS
                )
        
        # 计算暂停结束时间
        pause_until = now + timedelta(seconds=pause_duration)
        
        # 更新状态
        self._backoff_states[exchange_lower] = BackoffState(
            exchange=exchange_lower,
            error_type=error_type,
            error_count=error_count,
            last_error_time=now,
            pause_until=pause_until,
            pause_duration_seconds=pause_duration
        )

        # 如果是 nonce 错误，尝试触发局部重启钩子（节流）
        if error_type == ErrorType.INVALID_NONCE:
            hook = self._restart_hooks.get(exchange_lower)
            if hook:
                exchange_upper = exchange_lower.upper()
                last_ts = self._last_restart_ts.get(exchange_lower, 0)
                if now.timestamp() - last_ts >= 30:  # 至少间隔30秒
                    try:
                        hook()
                        self._last_restart_ts[exchange_lower] = now.timestamp()
                        self._log_throttled(
                            f"restart_{exchange_lower}",
                            f"🔄 [错误避让] {exchange_upper} 检测到 21104，已触发局部重启适配器",
                            interval_seconds=60,
                            level="warning"
                        )
                    except Exception as e:
                        logger.error(f"❌ [错误避让] {exchange_upper} 局部重启适配器失败: {e}")
        
        # 记录日志
        logger.warning(
            f"🚨 [错误避让] {exchange.upper()}: 检测到错误 {error_type.value} - {error_message}\n"
            f"   错误次数: {error_count}\n"
            f"   暂停时长: {pause_duration / 60:.1f} 分钟\n"
            f"   恢复时间: {pause_until.strftime('%H:%M:%S')}\n"
            f"   影响: ⏸️ 暂停开仓和平仓"
        )
    
    def is_paused(self, exchange: str) -> bool:
        """
        检查交易所是否处于暂停状态
        
        Args:
            exchange: 交易所名称
            
        Returns:
            True 表示暂停中，False 表示正常
        """
        exchange_lower = exchange.lower()
        state = self._backoff_states.get(exchange_lower)
        
        if state is None:
            return False
        
        now = datetime.now()
        
        # 检查是否还在暂停期内
        if now < state.pause_until:
            remaining_seconds = int((state.pause_until - now).total_seconds())
            error_reason = f"{state.error_type.value} 错误（连续 {state.error_count} 次）"
            
            # 🔥 节流警告日志（每60秒打印一次），用于决策引擎
            self._log_throttled(
                f"pause_warning_{exchange_lower}",
                f"⏸️ [错误避让] {exchange.upper()}: 处于错误避让期（{error_reason}，剩余{remaining_seconds}秒）",
                interval_seconds=60,
                level="warning"
            )
            return True
        
        # 暂停期已过，自动恢复
        # 🔥 只打印一次恢复日志，避免重复刷屏
        if not state.recovery_logged:
            logger.info(
                f"✅ [错误避让] {exchange.upper()}: 暂停期结束，恢复正常操作\n"
                f"   暂停原因: {state.error_type.value} 错误\n"
                f"   暂停时长: {state.pause_duration_seconds / 60:.1f} 分钟\n"
                f"   错误次数: {state.error_count}"
            )
            # 标记为已打印，避免重复
            state.recovery_logged = True
        
        # 不删除状态，保留错误计数（用于后续指数退避）
        # 只有超过 ERROR_RESET_SECONDS 才会重置
        return False
    
    def get_pause_info(self, exchange: str) -> Optional[Tuple[str, int, datetime]]:
        """
        获取暂停信息
        
        Args:
            exchange: 交易所名称
            
        Returns:
            (错误原因, 剩余秒数, 恢复时间) 或 None
        """
        exchange_lower = exchange.lower()
        state = self._backoff_states.get(exchange_lower)
        
        if state is None:
            return None
        
        now = datetime.now()
        if now >= state.pause_until:
            return None
        
        remaining_seconds = int((state.pause_until - now).total_seconds())
        error_reason = f"{state.error_type.value} 错误（连续 {state.error_count} 次）"
        
        return (error_reason, remaining_seconds, state.pause_until)
    
    def reset_exchange(self, exchange: str) -> None:
        """
        手动重置交易所的避让状态
        
        Args:
            exchange: 交易所名称
        """
        exchange_lower = exchange.lower()
        if exchange_lower in self._backoff_states:
            del self._backoff_states[exchange_lower]
            logger.info(f"🔄 [错误避让] {exchange.upper()}: 手动重置避让状态")
    
    def get_all_paused_exchanges(self) -> Dict[str, str]:
        """
        获取所有暂停中的交易所
        
        Returns:
            {exchange: reason}
        """
        now = datetime.now()
        paused = {}
        
        for exchange_lower, state in self._backoff_states.items():
            if now < state.pause_until:
                remaining_minutes = (state.pause_until - now).total_seconds() / 60
                reason = (
                    f"{state.error_type.value} 错误 "
                    f"(连续{state.error_count}次, 剩余{remaining_minutes:.1f}分钟)"
                )
                paused[exchange_lower] = reason
        
        return paused
    
    def _parse_error_type(self, error_code: str) -> Optional[ErrorType]:
        """
        解析错误类型
        
        Args:
            error_code: 错误码
            
        Returns:
            ErrorType 或 None
        """
        error_code_str = str(error_code).strip()
        
        if "21104" in error_code_str:
            return ErrorType.INVALID_NONCE
        if "429" in error_code_str:
            return ErrorType.RATE_LIMIT
        if "23000" in error_code_str or "too many requests" in error_code_str.lower():
            return ErrorType.RATE_LIMIT_L1

        return None
    
    def _log_throttled(self, key: str, message: str, interval_seconds: float = 60, level: str = "info"):
        """
        节流日志（避免刷屏）
        
        Args:
            key: 日志键
            message: 日志内容
            interval_seconds: 最小间隔（秒）
            level: 日志级别（info/warning），默认info
        """
        now = time.time()
        last_time = self._last_log_times.get(key, 0)
        
        if now - last_time >= interval_seconds:
            if level.lower() == "warning":
                logger.warning(message)
            else:
                logger.info(message)
            self._last_log_times[key] = now

    def set_restart_hook(self, exchange: str, hook: callable) -> None:
        """
        注册局部重启钩子（例如仅重建 lighter 的 REST/WS 连接）
        """
        if not exchange or not hook:
            return
        self._restart_hooks[exchange.lower()] = hook
