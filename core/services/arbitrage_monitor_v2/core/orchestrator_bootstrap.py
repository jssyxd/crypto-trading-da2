from __future__ import annotations

"""
Orchestrator 引导模块
---------------------
封装 `UnifiedOrchestrator` 初始化和交易所连接的纯流程逻辑：
- 创建并配置所有交易所适配器
- 建立连接、订阅行情
- 负责余额刷新等通用操作

拆分该模块可让调度器主类聚焦业务决策与调度顺序，降低函数体积并便于复用。
"""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Tuple, Any, Optional, List

import yaml
from decimal import Decimal

from core.adapters.exchanges.interface import ExchangeInterface
from core.adapters.exchanges.utils.setup_logging import LoggingConfig

if TYPE_CHECKING:
    from .unified_orchestrator import UnifiedOrchestrator


logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file="unified_orchestrator.log",
    console_formatter=None,
    file_formatter="detailed",
    level=logging.INFO,
)
logger.propagate = False


class OrchestratorBootstrap:
    """负责 orchestrator 初始化、交易所连接与订阅的辅助类。"""

    def __init__(self, orchestrator: "UnifiedOrchestrator") -> None:
        self.orchestrator = orchestrator

    def init_exchange_adapters(self) -> None:
        """初始化交易所适配器（参考原逻辑）。"""
        orc = self.orchestrator
        monitor_config = orc.monitor_config
        logger.info(
            f"🔧 [统一调度] 开始初始化交易所适配器: {monitor_config.exchanges}"
        )

        from core.adapters.exchanges.factory import ExchangeFactory
        from core.utils.config_loader import ExchangeConfigLoader

        factory = ExchangeFactory()
        config_loader = ExchangeConfigLoader()

        for exchange_name in monitor_config.exchanges:
            logger.info(f"🔧 [统一调度] 正在创建适配器: {exchange_name}")
            try:
                config_path = Path(f"config/exchanges/{exchange_name}_config.yaml")
                exchange_config = None

                if config_path.exists():
                    exchange_config = self._load_exchange_config(
                        exchange_name, config_path, config_loader
                    )

                adapter = factory.create_adapter(
                    exchange_id=exchange_name,
                    config=exchange_config,
                )

                if adapter:
                    orc.exchange_adapters[exchange_name] = adapter
                    logger.info(f"✅ [统一调度] 交易所适配器已创建: {exchange_name}")
                else:
                    logger.warning(
                        f"⚠️  [统一调度] 无法创建交易所适配器: {exchange_name}"
                    )
            except Exception as exc:
                logger.error(
                    f"❌ [统一调度] 创建交易所适配器失败 {exchange_name}: {exc}",
                    exc_info=True,
                )

    def _load_exchange_config(
        self,
        exchange_name: str,
        config_path: Path,
        config_loader,
    ):
        from core.adapters.exchanges.interface import ExchangeConfig
        from core.adapters.exchanges.models import ExchangeType

        type_map = {
            "edgex": ExchangeType.SPOT,
            "lighter": ExchangeType.SPOT,
            "hyperliquid": ExchangeType.PERPETUAL,
            "binance": ExchangeType.PERPETUAL,
            "backpack": ExchangeType.SPOT,
            "paradex": ExchangeType.PERPETUAL,
            "grvt": ExchangeType.PERPETUAL,
        }

        try:
            with open(config_path, "r", encoding="utf-8") as file:
                config_data = yaml.safe_load(file)
        except Exception as exc:
            logger.warning(
                f"⚠️  [{exchange_name}] 配置文件解析失败: {exc}，使用默认配置"
            )
            return None

        if exchange_name in config_data:
            config_data = config_data[exchange_name]

        api_config = config_data.get("api", {})
        authentication_config = config_data.get("authentication", {})
        extra_params = dict(config_data.get("extra_params", {}))

        auth = config_loader.load_auth_config(
            exchange_name,
            use_env=True,
            config_file=str(config_path),
        )

        api_key = auth.api_key or authentication_config.get("api_key") or config_data.get("api_key", "")
        api_secret = (
            auth.api_secret
            or auth.private_key
            or authentication_config.get("api_secret")
            or config_data.get("api_secret", "")
        )
        private_key = auth.private_key or authentication_config.get("private_key")
        wallet_address = (
            auth.wallet_address
            or config_data.get("wallet_address")
            or authentication_config.get("wallet_address")
        )
        if wallet_address:
            extra_params.setdefault("wallet_address", wallet_address)
        if auth.jwt_token:
            extra_params["jwt_token"] = auth.jwt_token
        if auth.l2_address:
            extra_params["l2_address"] = auth.l2_address
        if auth.sub_account_id:
            extra_params["sub_account_id"] = auth.sub_account_id

        exchange_config = ExchangeConfig(
            exchange_id=exchange_name,
            name=config_data.get("name", exchange_name),
            exchange_type=type_map.get(exchange_name, ExchangeType.SPOT),
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=config_data.get("api_passphrase") or auth.api_passphrase,
            private_key=private_key,
            wallet_address=wallet_address,
            testnet=config_data.get("testnet", False),
            base_url=api_config.get("base_url") or config_data.get("base_url"),
            ws_url=api_config.get("ws_url"),
            extra_params=extra_params,
        )

        if exchange_name == "edgex":
            account_id = auth.account_id or authentication_config.get("account_id")
            stark_private_key = auth.stark_private_key or authentication_config.get(
                "stark_private_key"
            )
            if account_id and stark_private_key:
                exchange_config.authentication = type(
                    "Auth",
                    (),
                    {"account_id": str(account_id), "stark_private_key": stark_private_key},
                )()
                private_ws_url = api_config.get("private_ws_url") or "wss://pro.edgex.exchange/api/v1/private/ws"
                exchange_config.private_ws_url = private_ws_url
                logger.info(
                    f"🔐 [EdgeX] 已注入认证信息: account_id={account_id}, private_ws_url={private_ws_url}"
                )

        if exchange_name == "backpack" and api_config.get("private_ws_url"):
            exchange_config.private_ws_url = api_config.get("private_ws_url")

        if exchange_name == "lighter" and auth:
            exchange_config.api_key_private_key = auth.api_key_private_key
            exchange_config.account_index = auth.account_index
            exchange_config.api_key_index = auth.api_key_index
            logger.info(
                f"🔑 [Lighter] 已加载认证配置: account_index={auth.account_index}, api_key_private_key_len={len(auth.api_key_private_key)}"
            )

        return exchange_config

    async def connect_all_exchanges(self) -> None:
        """连接所有交易所并订阅数据。"""
        orc = self.orchestrator
        data_receiver = orc.data_receiver
        logger.info("🔌 [统一调度] 正在连接交易所...")

        async def connect_adapter(exchange_name: str, adapter: ExchangeInterface):
            try:
                logger.info(f"🔌 [{exchange_name}] 开始连接...")
                if hasattr(adapter, "connect"):
                    await adapter.connect()
                else:
                    await adapter.start()
                logger.info(f"✅ [{exchange_name}] 连接成功，注册到数据接收层...")
                data_receiver.register_adapter(exchange_name, adapter)
                return (exchange_name, adapter, None)
            except Exception as exc:
                logger.error(f"❌ [{exchange_name}] 连接失败: {exc}", exc_info=True)
                return (exchange_name, None, exc)

        await asyncio.gather(
            *[
                connect_adapter(name, adapter)
                for name, adapter in orc.exchange_adapters.items()
            ],
            return_exceptions=True,
        )

        await self._subscribe_market_data()

    async def _subscribe_market_data(self) -> None:
        orc = self.orchestrator
        data_receiver = orc.data_receiver
        try:
            subscription_symbols = set(orc.monitor_config.symbols)

            if orc.multi_leg_pairs:
                for pair in orc.multi_leg_pairs:
                    subscription_symbols.add(pair.leg_primary.normalized_symbol())
                    subscription_symbols.add(pair.leg_secondary.normalized_symbol())
                logger.info(
                    f"🔧 [统一调度] 多腿套利额外订阅: {[pair.pair_id for pair in orc.multi_leg_pairs]}"
                )

            if orc.multi_exchange_pairs:
                for pair in orc.multi_exchange_pairs:
                    subscription_symbols.add(pair.normalized_symbol())
                logger.info(
                    "🔧 [统一调度] 多交易所套利额外订阅: %s",
                    [pair.trading_pair_id for pair in orc.multi_exchange_pairs],
                )

            subscription_symbols = list(subscription_symbols)
            await data_receiver.subscribe_all(subscription_symbols)
            logger.info(f"✅ [统一调度] 已订阅 {len(subscription_symbols)} 个交易对")
        except Exception as exc:
            logger.error(f"❌ [统一调度] 订阅市场数据失败: {exc}", exc_info=True)

    async def disconnect_all_exchanges(self) -> None:
        orc = self.orchestrator
        for exchange_name, adapter in orc.exchange_adapters.items():
            try:
                await adapter.disconnect()
                logger.info(f"✅ [统一调度] 已断开: {exchange_name}")
            except Exception as exc:
                logger.error(f"❌ [统一调度] 断开{exchange_name}失败: {exc}")

