#!/usr/bin/env python3
"""
套利监控系统 - 主程序

实时监控多交易所价差和资金费率，识别套利机会。
"""

# 🔥 加载环境变量（必须在其他导入之前）
from dotenv import load_dotenv
from pathlib import Path as EnvPath
env_path = EnvPath(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)

from core.services.arbitrage_monitor.utils import SimpleSymbolConverter
from core.adapters.exchanges.factory import get_exchange_factory
from core.services.arbitrage_monitor import ArbitrageMonitorService, ArbitrageConfig
from rich.layout import Layout
from rich.live import Live
from rich import box
from rich.text import Text
from rich.panel import Panel
from rich.table import Table
from rich.console import Console, Group
import asyncio
import sys
import signal
import logging
import yaml
import os
import time
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from collections import deque
from typing import Optional, Dict, Any
from logging.handlers import RotatingFileHandler

# 🔥 网络流量监控（使用psutil）
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))


# 🔥 极简符号转换器（套利系统专用）


class UILogHandler(logging.Handler):
    """
    UI日志处理器 - 将日志捕获到队列中供UI显示
    
    关键特性：
    - 线程安全（使用deque）
    - 固定大小队列（自动淘汰旧日志）
    - 简化格式（移除冗余信息）
    """
    
    def __init__(self, log_queue: deque, max_size: int = 20):
        super().__init__()
        self.log_queue = log_queue
        self.max_size = max_size
        
    def emit(self, record: logging.LogRecord):
        """捕获日志记录"""
        try:
            # 格式化日志消息（简化格式）
            msg = self.format(record)
            
            # 添加到队列（保持最新N条）
            self.log_queue.append({
                'time': datetime.fromtimestamp(record.created).strftime('%H:%M:%S'),
                'level': record.levelname,
                'module': record.name.split('.')[-1] if '.' in record.name else record.name,
                'message': msg,
            })
            
            # 保持队列大小
            while len(self.log_queue) > self.max_size:
                self.log_queue.popleft()
        except Exception:
            # 忽略处理日志时的错误，避免死循环
            pass


class ArbitrageMonitorApp:
    """套利监控应用"""
    
    def __init__(self, config_path: str = "config/arbitrage/monitor.yaml"):
        self.config_path = config_path
        self.config = None
        self.adapters = {}
        self.monitor_service = None
        self.symbol_converter = None  # 🔥 符号转换服务
        self.console = Console()
        self.running = False
        
        # 🔥 日志捕获系统
        self.log_queue: deque = deque(maxlen=20)
        self.ui_log_handler: Optional[UILogHandler] = None
        
        # 🔥 排序缓存系统（每分钟更新一次排序）
        self.last_sort_time: Optional[datetime] = None
        self.sorted_symbols_cache: list = []  # 缓存排序后的symbol顺序
        self.sort_interval_seconds: int = 60  # 排序更新间隔（秒）
        
        # 🔥 费率差异持续时间跟踪系统
        # {symbol: {start_time, last_diff}}
        self.rate_diff_tracking: Dict[str, Dict[str, Any]] = {}
        self.rate_diff_threshold: float = 50.0  # 年化费率差阈值（百分比）
        
        # 🔥 网络流量监控（使用psutil）
        self.network_stats_enabled = PSUTIL_AVAILABLE
        self.process = None
        self.network_start_time = None
        self.network_start_bytes_sent = 0
        self.network_start_bytes_recv = 0
        if self.network_stats_enabled:
            try:
                self.process = psutil.Process(os.getpid())
                # 🔥 使用psutil的网络IO统计（而不是磁盘IO）
                net_io = psutil.net_io_counters()
                self.network_start_time = time.time()
                self.network_start_bytes_sent = net_io.bytes_sent
                self.network_start_bytes_recv = net_io.bytes_recv
            except Exception as e:
                self.logger.warning(f"⚠️  网络流量监控初始化失败: {e}")
                self.network_stats_enabled = False
        
        # 设置日志（先基础配置）
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger("arbitrage_monitor")
        
        # 🔥 设置日志捕获（在初始化后会被禁用控制台输出）
        self._setup_log_capture()
    
    def _setup_log_capture(self):
        """设置日志捕获并禁用控制台输出"""
        try:
            # 🔥 创建日志目录（如果不存在）
            log_dir = Path(__file__).parent / "logs"
            log_dir.mkdir(exist_ok=True)
            
            # 🔥 创建文件日志处理器（写入文件）
            log_file = log_dir / "arbitrage_monitor.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5,
                encoding='utf-8'
            )
            file_handler.setLevel(logging.INFO)
            file_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            file_handler.setFormatter(file_formatter)
            
            # 创建UI日志处理器
            self.ui_log_handler = UILogHandler(self.log_queue, max_size=20)
            self.ui_log_handler.setLevel(logging.INFO)
            
            # 简化日志格式（UI表格会显示时间、级别、模块）
            formatter = logging.Formatter('%(message)s')
            self.ui_log_handler.setFormatter(formatter)
            
            # 关键模块列表（需要捕获日志的模块）
            key_modules = [
                'arbitrage_monitor',
                'core.services.arbitrage_monitor',
                'core.adapters.exchanges.adapters.edgex_websocket',
                'core.adapters.exchanges.adapters.lighter_websocket',
                'ExchangeAdapter.edgex',  # 🔥 EdgeX适配器的logger名称
                'ExchangeAdapter.lighter',  # Lighter适配器的logger名称
            ]
            
            # 🔥 为root logger添加文件处理器（捕获所有日志）
            root_logger = logging.getLogger()
            if file_handler not in root_logger.handlers:
                root_logger.addHandler(file_handler)
            
            # 为每个关键模块配置日志
            for module_name in key_modules:
                module_logger = logging.getLogger(module_name)
                
                # 🔥 设置logger级别（确保至少是INFO）
                module_logger.setLevel(logging.INFO)
                
                # 🔥 添加文件日志处理器（写入文件）
                if file_handler not in module_logger.handlers:
                    module_logger.addHandler(file_handler)
                
                # 🔥 确保propagate=True，让日志也能传播到root logger（如果logger没有自己的handler）
                # 注意：如果logger已经有handler（如ExchangeAdapter.edgex），propagate=False也可以
                # 但我们需要确保文件handler已添加
                if not any(isinstance(h, RotatingFileHandler) for h in module_logger.handlers):
                    # 如果没有文件handler，添加一个
                    module_logger.addHandler(file_handler)
                
                # 添加UI日志处理器
                if self.ui_log_handler not in module_logger.handlers:
                    module_logger.addHandler(self.ui_log_handler)
                
        except Exception as e:
            self.logger.warning(f"设置日志捕获失败: {e}")
    
    def _disable_console_logging(self):
        """禁用控制台日志输出（在UI启动前调用）"""
        try:
            key_modules = [
                'arbitrage_monitor',
                'core.services.arbitrage_monitor',
                'core.adapters.exchanges.adapters.edgex_websocket',
                'core.adapters.exchanges.adapters.lighter_websocket',
                'ExchangeAdapter.edgex',  # 🔥 EdgeX适配器的logger名称
                'ExchangeAdapter.lighter',  # Lighter适配器的logger名称
            ]
            
            # 禁用root logger的控制台输出
            root_logger = logging.getLogger()
            for handler in root_logger.handlers[:]:
                if isinstance(handler, logging.StreamHandler) and \
                   not isinstance(handler, RotatingFileHandler):
                    root_logger.removeHandler(handler)
            
            # 为每个关键模块移除控制台输出
            for module_name in key_modules:
                module_logger = logging.getLogger(module_name)
                
                # 移除控制台输出handler（保留文件输出）
                for handler in module_logger.handlers[:]:
                    if isinstance(handler, logging.StreamHandler) and \
                       not isinstance(handler, RotatingFileHandler):
                        module_logger.removeHandler(handler)
                
                # 禁用传播到root logger
                module_logger.propagate = False
                
        except Exception as e:
            print(f"禁用控制台日志失败: {e}")
    
    def _format_log_message(self, message: str) -> str:
        """格式化日志消息（移除emoji）"""
        emoji_map = {
            '✅ ': '', '❌ ': '', '⚠️ ': '', '📝 ': '', 
            '📨 ': '', '🔄 ': '', '🔗 ': '', '💓 ': '',
            '📦 ': '', '📊 ': '', '🔍 ': '', '🚀 ': '',
            '🔌 ': '', '⚡ ': '', '🎯 ': ''
        }
        for emoji, replacement in emoji_map.items():
            message = message.replace(emoji, replacement)
        return message
    
    def _format_duration(self, seconds: float) -> str:
        """
        格式化持续时间为 1D1H1M 格式
        
        Args:
            seconds: 持续秒数
            
        Returns:
            格式化的时间字符串，例如：1D2H30M
        """
        if seconds < 60:
            return "-"  # 少于1分钟不显示
        
        total_seconds = int(seconds)
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        
        parts = []
        if days > 0:
            parts.append(f"{days}D")
        if hours > 0:
            parts.append(f"{hours}H")
        if minutes > 0:
            parts.append(f"{minutes}M")
        
        return "".join(parts) if parts else "-"
    
    def _update_rate_diff_tracking(self, symbol: str, rate_diff_annual: float):
        """
        更新费率差异持续时间跟踪
        
        Args:
            symbol: 交易对
            rate_diff_annual: 年化费率差（百分比）
        """
        current_time = datetime.now()
        abs_diff = abs(rate_diff_annual)
        
        if abs_diff >= self.rate_diff_threshold:
            # 费率差大于阈值
            if symbol not in self.rate_diff_tracking:
                # 首次超过阈值，开始记录
                self.rate_diff_tracking[symbol] = {
                    'start_time': current_time,
                    'last_diff': rate_diff_annual
                }
            else:
                # 更新最后差异值
                self.rate_diff_tracking[symbol]['last_diff'] = rate_diff_annual
        else:
            # 费率差低于阈值，清除记录
            if symbol in self.rate_diff_tracking:
                del self.rate_diff_tracking[symbol]
    
    def _get_rate_diff_duration(self, symbol: str) -> str:
        """
        获取费率差异持续时间
        
        Args:
            symbol: 交易对
            
        Returns:
            格式化的持续时间字符串
        """
        if symbol not in self.rate_diff_tracking:
            return "-"
        
        start_time = self.rate_diff_tracking[symbol]['start_time']
        duration_seconds = (datetime.now() - start_time).total_seconds()
        
        return self._format_duration(duration_seconds)
    
    def _get_network_stats(self) -> Dict[str, Any]:
        """
        获取网络流量统计
        
        Returns:
            包含网络流量信息的字典
        """
        if not self.network_stats_enabled or not self.process:
            return {"enabled": False}
        
        try:
            # 🔥 使用psutil的网络IO统计（而不是磁盘IO）
            net_io = psutil.net_io_counters()
            current_time = time.time()
            
            # 计算总流量（从启动开始）
            total_sent = net_io.bytes_sent - self.network_start_bytes_sent
            total_recv = net_io.bytes_recv - self.network_start_bytes_recv
            total_bytes = total_sent + total_recv
            
            # 计算运行时间
            elapsed_seconds = current_time - self.network_start_time if self.network_start_time else 0
            
            # 计算平均速率（字节/秒）
            avg_sent_rate = total_sent / elapsed_seconds if elapsed_seconds > 0 else 0
            avg_recv_rate = total_recv / elapsed_seconds if elapsed_seconds > 0 else 0
            avg_total_rate = avg_sent_rate + avg_recv_rate
            
            def format_bytes(bytes_count: float) -> str:
                """格式化字节数为可读格式"""
                if bytes_count < 1024:
                    return f"{bytes_count:.0f}B"
                elif bytes_count < 1024 * 1024:
                    return f"{bytes_count / 1024:.2f}KB"
                elif bytes_count < 1024 * 1024 * 1024:
                    return f"{bytes_count / (1024 * 1024):.2f}MB"
                else:
                    return f"{bytes_count / (1024 * 1024 * 1024):.2f}GB"
            
            def format_rate(bytes_per_sec: float) -> str:
                """格式化速率为可读格式"""
                if bytes_per_sec < 1024:
                    return f"{bytes_per_sec:.0f}B/s"
                elif bytes_per_sec < 1024 * 1024:
                    return f"{bytes_per_sec / 1024:.2f}KB/s"
                else:
                    return f"{bytes_per_sec / (1024 * 1024):.2f}MB/s"
            
            return {
                "enabled": True,
                "total_sent": format_bytes(total_sent),
                "total_recv": format_bytes(total_recv),
                "total_bytes": format_bytes(total_bytes),
                "avg_sent_rate": format_rate(avg_sent_rate),
                "avg_recv_rate": format_rate(avg_recv_rate),
                "avg_total_rate": format_rate(avg_total_rate),
            }
        except Exception as e:
            self.logger.debug(f"获取网络流量统计失败: {e}")
            return {"enabled": False, "error": str(e)}
    
    def load_config(self):
        """加载配置"""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        self.logger.info("✅ 配置加载成功")
    
    async def initialize(self):
        """初始化"""
        print("\n" + "="*60)
        print("🚀 套利监控系统启动中...")
        print("="*60 + "\n")
        
        # 🔥 第1步：创建极简符号转换器（无需配置文件）
        self.symbol_converter = SimpleSymbolConverter(self.logger)
        self.logger.info("✅ 极简符号转换器就绪（~150行代码，零冗余）")
        
        # 第2步：初始化交易所适配器
        self.logger.info("🔌 初始化交易所适配器...")
        print("🔌 正在初始化交易所适配器...\n")
        
        # 🔥 用于收集所有交易所支持的symbol
        exchange_symbols = {}
        
        factory = get_exchange_factory()
        
        for exchange_name in self.config['exchanges']:
            adapter = None
            try:
                # 尝试从配置文件加载（包含API密钥）
                try:
                    adapter = await factory.create_adapter(exchange_name)
                    await adapter.connect()
                    self.adapters[exchange_name] = adapter
                    self.logger.info(f"✅ {exchange_name} 初始化成功（使用配置文件）")
                except Exception as config_error:
                    # 如果配置文件失败，尝试"公开数据模式"（无需API密钥）
                    self.logger.warning(
                        f"⚠️  {exchange_name} 配置文件加载失败: {config_error}")
                    self.logger.info(f"🔄 尝试公开数据模式...")
                    
                    # 创建虚拟配置（用于公开数据访问）
                    from core.adapters.exchanges.interface import ExchangeConfig
                    from core.adapters.exchanges.models import ExchangeType
                    
                    dummy_config = ExchangeConfig(
                        exchange_id=exchange_name,
                        name=exchange_name.capitalize(),
                        exchange_type=ExchangeType.PERPETUAL,  # 套利监控主要用永续合约
                        api_key="public_data_only",
                        api_secret="public_data_only",
                        testnet=False
                    )
                    
                    # 根据交易所类型创建适配器
                    if exchange_name == 'backpack':
                        from core.adapters.exchanges.adapters.backpack import BackpackAdapter
                        adapter = BackpackAdapter(dummy_config)
                    elif exchange_name == 'edgex':
                        from core.adapters.exchanges.adapters.edgex import EdgeXAdapter
                        adapter = EdgeXAdapter(dummy_config)
                    elif exchange_name == 'lighter':
                        from core.adapters.exchanges.adapters.lighter import LighterAdapter
                        adapter = LighterAdapter(dummy_config)
                    else:
                        raise ValueError(f"不支持的交易所: {exchange_name}")
                    
                    # 只连接WebSocket（不进行认证）
                    await adapter.connect()
                    self.adapters[exchange_name] = adapter
                    self.logger.info(f"✅ {exchange_name} 初始化成功（公开数据模式）")
                
                # 🔥 获取该交易所支持的symbol（转换为标准格式）- 无论哪种模式都执行
                if adapter:
                    try:
                        # 🔥 EdgeX特殊处理：主动获取交易对
                        if exchange_name == 'edgex':
                            self.logger.info("⏳ 正在获取EdgeX支持的交易对...")
                            print("⏳ 正在获取EdgeX交易对列表（约10秒）...")
                            # 调用 fetch_supported_symbols() 来真正获取交易对
                            if hasattr(adapter, 'websocket') and hasattr(adapter.websocket, 'fetch_supported_symbols'):
                                await adapter.websocket.fetch_supported_symbols()
                            elif hasattr(adapter, '_websocket') and hasattr(adapter._websocket, 'fetch_supported_symbols'):
                                await adapter._websocket.fetch_supported_symbols()
                            else:
                                await asyncio.sleep(12)  # 降级方案：等待metadata自动到达
                        
                        raw_symbols = await adapter.get_supported_symbols()
                        self.logger.info(
                            f"🔍 {exchange_name} 原始symbols数量: {len(raw_symbols)}")
                        
                        if len(raw_symbols) == 0:
                            print(f"❌ {exchange_name}: 未获取到交易对！")
                            self.logger.warning(f"{exchange_name} 未获取到任何交易对")
                        else:
                            # 🔥 显示前5个原始symbol（调试用）
                            sample_raw = raw_symbols[:5] if len(
                                raw_symbols) >= 5 else raw_symbols
                            print(f"   📋 前5个原始symbol: {', '.join(sample_raw)}")
                        
                        standard_symbols = set()
                        for raw_symbol in raw_symbols:
                            try:
                                std_symbol = self.symbol_converter.convert_from_exchange(
                                    raw_symbol, exchange_name)
                                # 永续合约
                                if std_symbol.endswith('-PERP') or std_symbol.endswith('-USDC-PERP'):
                                    standard_symbols.add(std_symbol)
                            except Exception as convert_error:
                                # 转换失败，忽略
                                pass
                        
                        exchange_symbols[exchange_name] = standard_symbols
                        self.logger.info(
                            f"📊 {exchange_name} 支持 {len(standard_symbols)} 个永续合约")
                        
                        if len(standard_symbols) > 0:
                            print(
                                f"✅ {exchange_name}: 发现 {len(raw_symbols)} 个交易对 → {len(standard_symbols)} 个永续合约")
                        else:
                            print(
                                f"⚠️  {exchange_name}: {len(raw_symbols)} 个交易对中没有永续合约")
                    except Exception as e:
                        self.logger.error(
                            f"⚠️  无法获取 {exchange_name} 支持的symbol: {e}")
                        print(f"❌ {exchange_name}: 获取symbol失败 - {e}")  # 临时调试
                        import traceback
                        self.logger.error(traceback.format_exc())
                        exchange_symbols[exchange_name] = set()
                    
            except Exception as e:
                self.logger.error(f"❌ {exchange_name} 初始化失败: {e}")
                import traceback
                self.logger.debug(traceback.format_exc())
        
        if not self.adapters:
            raise RuntimeError("❌ 没有可用的交易所适配器，请检查：\n"
                             "  1. 配置文件是否正确：config/exchanges/\n"
                             "  2. API密钥是否有效\n"
                             "  3. 网络连接是否正常")
        
        # 🔥 第3步：计算重叠的symbol
        print(f"\n📊 交易所symbol统计:")
        self.logger.info(
            f"📊 exchange_symbols 字典内容: {list(exchange_symbols.keys())}")
        for ex_name, symbols in exchange_symbols.items():
            print(f"   {ex_name}: {len(symbols)} 个永续合约")
            self.logger.info(f"   - {ex_name}: {len(symbols)} 个symbol")
        
        if len(exchange_symbols) >= 2:
            # 计算交集
            common_symbols = set.intersection(
                *exchange_symbols.values()) if exchange_symbols else set()
            print(f"\n🔍 发现 {len(common_symbols)} 个重叠永续合约")
            self.logger.info(f"🔍 发现 {len(common_symbols)} 个重叠symbol")
            
            if common_symbols:
                # 显示前10个重叠symbol
                sample_common = sorted(list(common_symbols))[:10]
                print(f"   前10个: {', '.join(sample_common)}")
                self.logger.info(f"   示例: {', '.join(sample_common)}")
            
            # 如果有重叠symbol，使用它们；否则使用配置文件中的
            if common_symbols:
                # 排序（不限制数量）
                sorted_symbols = sorted(list(common_symbols))
                self.config['symbols'] = sorted_symbols  # 使用所有重叠symbol
                print(f"✅ 最终监控 {len(self.config['symbols'])} 个交易对\n")
                self.logger.info(
                    f"✅ 使用 {len(self.config['symbols'])} 个重叠symbol")
                self.logger.info(
                    f"   前10个: {', '.join(self.config['symbols'][:10])}")
            else:
                print("⚠️  没有发现重叠symbol，使用配置文件中的symbol\n")
                self.logger.warning("⚠️  没有发现重叠symbol，使用配置文件中的symbol")
        else:
            print(f"⚠️  交易所数量不足（{len(exchange_symbols)}），需要至少2个\n")
            self.logger.warning(
                f"⚠️  exchange_symbols 数量不足（{len(exchange_symbols)}），需要至少2个")
        
        # 第4步：创建监控服务
        print(f"\n🔧 创建监控服务配置:")
        print(f"   交易所: {list(self.adapters.keys())}")
        print(f"   监控交易对数量: {len(self.config['symbols'])} 个")
        print(f"   前10个: {', '.join(self.config['symbols'][:10])}")
        
        arbitrage_config = ArbitrageConfig(
            exchanges=list(self.adapters.keys()),
            symbols=self.config['symbols'],
            price_spread_threshold=Decimal(
                str(self.config['thresholds']['price_spread'])),
            funding_rate_threshold=Decimal(
                str(self.config['thresholds']['funding_rate'])),
            min_score_threshold=Decimal(
                str(self.config['thresholds']['min_score'])),
            update_interval=self.config['monitoring']['update_interval'],
            refresh_rate=self.config['display']['refresh_rate'],
            max_opportunities=self.config['display']['max_opportunities'],
            show_all_prices=self.config['display']['show_all_prices']
        )
        
        self.monitor_service = ArbitrageMonitorService(
            adapters=self.adapters,
            config=arbitrage_config,
            logger=self.logger,
            symbol_converter=self.symbol_converter  # 🔥 传递符号转换服务
        )
        
        await self.monitor_service.start()
        self.logger.info("✅ 套利监控服务启动成功")
    
    def _get_price_precision(self, price: float) -> int:
        """
        根据价格大小动态决定显示精度
        
        Args:
            price: 价格
            
        Returns:
            小数位数
        """
        if price >= 1000:
            return 2  # 大币种：BTC, ETH 等 → 100,204.00
        elif price >= 10:
            return 3  # 中等价格 → 39.123
        elif price >= 1:
            return 4  # 接近1的价格 → 2.8456
        elif price >= 0.01:
            return 6  # 小价格 → 0.012345
        else:
            return 8  # 极小价格 → 0.00012345
    
    def create_logs_table(self) -> Panel:
        """创建日志表格"""
        table = Table(show_header=True, box=None, padding=(0, 1))
        
        # 定义列
        table.add_column("时间", style="dim", width=8, no_wrap=True)
        table.add_column("级别", style="bold", width=6, no_wrap=True)
        table.add_column("模块", style="cyan", width=15, no_wrap=True)
        table.add_column("消息", style="white")  # 无长度限制，完整显示
        
        # 如果没有日志，显示提示
        if not self.log_queue:
            table.add_row("--:--:--", "--", "等待日志", "[dim]暂无日志[/dim]")
        else:
            # 显示最新20条日志
            for log_entry in list(self.log_queue):
                # 根据日志级别设置颜色
                level = log_entry['level']
                if level == 'ERROR':
                    level_style = "[bold red]ERROR[/bold red]"
                elif level == 'WARNING':
                    level_style = "[bold yellow]WARN[/bold yellow]"
                elif level == 'INFO':
                    level_style = "[bold green]INFO[/bold green]"
                elif level == 'DEBUG':
                    level_style = "[dim]DEBUG[/dim]"
                else:
                    level_style = level
                
                # 格式化消息（移除emoji）
                message = self._format_log_message(log_entry['message'])
                
                table.add_row(
                    log_entry['time'],
                    level_style,
                    log_entry['module'][:15],  # 限制模块名长度
                    message
                )
        
        # 返回Panel（固定高度：1标题+1表头+20数据+1边框=23）
        return Panel(
            table, 
            title="📋 最新日志 (最新20条)", 
            border_style="blue", 
            height=23
        )
    
    def create_header(self) -> Panel:
        """创建标题栏"""
        title_text = Text()
        title_text.append("🎯 ", style="bold yellow")
        title_text.append("套利监控系统", style="bold green")
        title_text.append(" - ", style="dim")
        title_text.append(datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"), style="bold cyan")
        
        # 🔥 显示下次排序倒计时
        if self.last_sort_time is not None:
            time_since_sort = (
                datetime.now() - self.last_sort_time).total_seconds()
            time_until_next_sort = self.sort_interval_seconds - time_since_sort
            if time_until_next_sort > 0:
                title_text.append(" | ", style="dim")
                title_text.append(
                    f"下次排序: {int(time_until_next_sort)}秒", style="bold magenta")
            else:
                title_text.append(" | ", style="dim")
                title_text.append("正在排序...", style="bold yellow")
        
        return Panel(
            title_text,
            border_style="green",
            padding=(0, 1)
        )
    
    def create_controls_panel(self) -> Panel:
        """创建控制命令面板"""
        controls_text = Text()
        controls_text.append("按 ", style="dim")
        controls_text.append("Ctrl+C", style="bold red")
        controls_text.append(" 退出程序", style="dim")
        
        return Panel(
            controls_text,
            border_style="white",
            padding=(0, 1)
        )
    
    def generate_display(self) -> Layout:
        """生成显示内容（使用Layout布局）"""
        layout = Layout()
        
        if not self.monitor_service:
            # 初始化布局
            layout.split_column(
                Layout(self.create_header(), size=3),
                Layout(Panel("等待初始化...", border_style="yellow")),
                Layout(self.create_logs_table(), size=23),
                Layout(self.create_controls_panel(), size=3)
            )
            return layout
        
        # 统计信息
        stats = self.monitor_service.get_statistics()
        stats_text = Text()
        stats_text.append(
            f"交易所: {stats['total_exchanges']}  ", style="bold cyan")
        stats_text.append(
            f"监控: {stats['monitored_symbols']}对  ", style="bold green")
        stats_text.append(
            f"机会: {stats['active_opportunities']}  ", style="bold yellow")
        stats_text.append(
            f"数据: {stats['ticker_data_count']}", style="bold magenta")

        # 🔥 显示各交易所连接健康状态
        if 'exchange_health' in stats:
            stats_text.append("\n", style="")
            for exchange_name, health in stats['exchange_health'].items():
                # 状态图标和颜色
                status = health['status']
                if status == 'healthy':
                    status_icon = "✅"
                    status_style = "bold green"
                elif status == 'degraded':
                    status_icon = "⚠️"
                    status_style = "bold yellow"
                elif status == 'reconnecting':
                    status_icon = "🔄"
                    status_style = "bold blue"
                else:  # unhealthy
                    status_icon = "❌"
                    status_style = "bold red"

                # 显示交易所名称和健康比例
                stats_text.append(f"{exchange_name}: ", style="bold cyan")
                stats_text.append(
                    f"{status_icon} {health['healthy_count']}/{health['total_count']} ",
                    style=status_style
                )
        
                # 显示重连次数（如果有）
                if health['reconnect_count'] > 0:
                    stats_text.append(
                        f"(重连×{health['reconnect_count']}) ",
                        style="dim yellow"
                    )

                stats_text.append("  ", style="")
        
        # 🔥 显示网络流量统计（置顶位置）
        network_stats = self._get_network_stats()
        if network_stats.get("enabled"):
            stats_text.append("\n", style="")
            stats_text.append("📡 网络流量: ", style="bold cyan")
            stats_text.append(
                f"↑{network_stats['total_sent']} ",
                style="bold yellow"
            )
            stats_text.append(
                f"↓{network_stats['total_recv']} ",
                style="bold green"
            )
            stats_text.append(
                f"({network_stats['avg_total_rate']})",
                style="dim"
            )
        elif not PSUTIL_AVAILABLE:
            # 🔥 如果psutil不可用，显示提示信息
            stats_text.append("\n", style="")
            stats_text.append("📡 网络流量: ", style="bold cyan")
            stats_text.append(
                "[dim]未启用 (需要安装psutil: pip install psutil)[/dim]",
                style="dim"
            )
        
        # 🚀 显示性能指标（队列状态和处理延迟）
        if 'performance_metrics' in stats:
            metrics = stats['performance_metrics']
            stats_text.append("\n", style="")
            stats_text.append("⚡ 性能指标: ", style="bold cyan")
            
            # 队列积压情况
            orderbook_q = metrics.get('orderbook_queue_size', 0)
            ticker_q = metrics.get('ticker_queue_size', 0)
            analysis_q = metrics.get('analysis_queue_size', 0)
            
            # 根据队列大小显示不同颜色
            q_style = "bold green" if (orderbook_q + ticker_q < 50) else "bold yellow" if (orderbook_q + ticker_q < 200) else "bold red"
            stats_text.append(
                f"队列[订单簿:{orderbook_q} Ticker:{ticker_q} 分析:{analysis_q}] ",
                style=q_style
            )
            
            # 分析延迟
            latency = metrics.get('last_analysis_latency_ms', 0)
            latency_style = "bold green" if latency < 50 else "bold yellow" if latency < 100 else "bold red"
            stats_text.append(
                f"分析延迟:{latency:.1f}ms ",
                style=latency_style
            )
            
            # 处理量统计
            orderbook_processed = metrics.get('orderbook_processed', 0)
            ticker_processed = metrics.get('ticker_processed', 0)
            stats_text.append(
                f"[已处理 订单簿:{orderbook_processed} Ticker:{ticker_processed}]",
                style="dim"
            )

        # 🔥 套利机会只记录到日志，不显示表格（用户要求：表格太占空间）
        opportunities = self.monitor_service.get_opportunities()

        # 记录套利机会到日志（供文件查看）
        if opportunities:
            opp_count = len(opportunities)
            # 只记录评分最高的前3条到日志
            for opp in opportunities[:3]:
                type_str = "价差" if opp.opportunity_type == "price_spread" else \
                          "费率" if opp.opportunity_type == "funding_rate" else "组合"
                
                if opp.price_spread:
                    buy_ex = opp.price_spread.exchange_buy
                    sell_ex = opp.price_spread.exchange_sell
                    spread_pct = f"{float(opp.price_spread.spread_pct):.3f}%"
                elif opp.funding_rate_spread:
                    buy_ex = opp.funding_rate_spread.exchange_low
                    sell_ex = opp.funding_rate_spread.exchange_high
                    spread_pct = f"{float(opp.funding_rate_spread.spread_abs * 100):.3f}%"
                else:
                    buy_ex = sell_ex = spread_pct = "-"
                
                score = f"{float(opp.score):.4f}"

                # 记录到日志文件
                self.logger.info(
                    f"套利机会: {opp.symbol} | {type_str} | 买入:{buy_ex} 卖出:{sell_ex} | 价差:{spread_pct} | 评分:{score}")
        
        # 价格表格
        if self.config['display']['show_all_prices']:
            # 🔥 添加数据就绪状态提示
            total_symbols = len(self.config['symbols'])
            ready_symbols = len(
                [s for s in self.config['symbols'] if self.monitor_service.get_current_prices(s)])
            data_ready_pct = (ready_symbols / total_symbols *
                              100) if total_symbols > 0 else 0
            
            if data_ready_pct < 100:
                price_table_title = f"💰 实时价格 & 资金费率 [数据准备中: {ready_symbols}/{total_symbols} ({data_ready_pct:.0f}%)]"
            else:
                price_table_title = "💰 实时价格 & 资金费率"
            
            price_table = Table(title=price_table_title, box=box.SIMPLE,
                                show_header=True, header_style="bold cyan")
            price_table.add_column("交易对", style="cyan", width=18)  # 🔥 宽屏优化：从15增加到18
            
            # 🔥 改造：显示买1/卖1价格和数量（宽屏优化）
            for exchange in self.config['exchanges']:
                price_table.add_column(
                    f"{exchange.upper()}\n买1/卖1", justify="right", width=36)  # 🔥 宽度从24增加到36，适配宽屏
                if self.config['display'].get('show_funding_rates', True):
                    price_table.add_column(
                        f"{exchange.upper()}\n8h/年化", justify="right", width=18)  # 🔥 宽度从16增加到18
            
            price_table.add_column("价差%", style="yellow",
                                   justify="right", width=12)  # 🔥 宽度从10增加到12
            
            # 🔥 添加费率差列（8小时 + 年化）
            if self.config['display'].get('show_funding_rates', True) and len(self.config['exchanges']) >= 2:
                price_table.add_column(
                    "费率差\n8h/年化", style="magenta", justify="right", width=20)  # 🔥 宽度从16增加到20
                # 🔥 添加持续时间列（当年化差>50%时显示）
                price_table.add_column(
                    "持续\n时间", style="bold red", justify="center", width=10)  # 🔥 宽度从8增加到10
                # 🔥 添加同向列
                price_table.add_column(
                    "同向", style="bold cyan", justify="center", width=8)  # 🔥 宽度从6增加到8
            
            # 🔥 第1步：收集所有数据并计算价差（实时数据）
            symbol_data_dict = {}  # 使用dict方便按symbol查找
            
            for symbol in self.config['symbols']:
                # 🔥 获取订单簿价格（改造后返回 {exchange: {"bid": ..., "ask": ..., ...}}）
                orderbook_prices = self.monitor_service.get_current_prices(symbol)
                if not orderbook_prices:
                    continue
                
                # 获取funding_rates
                funding_rates = {}
                ticker_data = self.monitor_service.ticker_data
                for exchange in self.config['exchanges']:
                    if exchange in ticker_data and symbol in ticker_data[exchange]:
                        funding_rate = ticker_data[exchange][symbol].funding_rate
                        funding_rates[exchange] = funding_rate
                
                # 🔥 计算有利可图的价差（用于排序）
                # 只计算正向套利机会（买1价 > 卖1价）
                spread_value = 0
                if len(orderbook_prices) >= 2:
                    # 尝试所有交易所两两组合，找到最大正价差
                    from itertools import combinations
                    for ex1, ex2 in combinations(orderbook_prices.keys(), 2):
                        book1 = orderbook_prices[ex1]
                        book2 = orderbook_prices[ex2]
                        
                        # 正向套利1：在ex1买入（ask1），在ex2卖出（bid2）
                        if book2["bid"] > book1["ask"]:
                            spread = float(((book2["bid"] - book1["ask"]) / book1["ask"]) * Decimal("100"))
                            spread_value = max(spread_value, spread)
                        
                        # 正向套利2：在ex2买入（ask2），在ex1卖出（bid1）
                        if book1["bid"] > book2["ask"]:
                            spread = float(((book1["bid"] - book2["ask"]) / book2["ask"]) * Decimal("100"))
                            spread_value = max(spread_value, spread)
                
                # 保存数据（使用dict，key为symbol）
                symbol_data_dict[symbol] = {
                    'symbol': symbol,
                    'orderbook_prices': orderbook_prices,  # 🔥 改为订单簿价格
                    'funding_rates': funding_rates,
                    'spread_value': spread_value  # 🔥 只保存有利可图的价差
                }
            
            # 🔥 第2步：检查是否需要重新排序（每60秒更新一次排序）
            current_time = datetime.now()
            need_resort = False
            
            if self.last_sort_time is None:
                # 首次运行，需要排序
                need_resort = True
                self.logger.info("首次排序价格表格")
            else:
                # 检查距离上次排序是否超过60秒
                time_since_last_sort = (
                    current_time - self.last_sort_time).total_seconds()
                if time_since_last_sort >= self.sort_interval_seconds:
                    need_resort = True
                    self.logger.info(
                        f"距离上次排序已过 {time_since_last_sort:.0f} 秒，重新排序")
            
            # 🔥 优化：如果有数据且需要排序，立即排序；如果是首次且数据少，也先排序显示
            if len(symbol_data_dict) > 0 and (need_resort or (self.last_sort_time is None and len(symbol_data_dict) >= 3)):
                # 需要重新排序：按价差从高到低排序
                symbol_data_list = list(symbol_data_dict.values())

                # 🔥 自定义排序：BTC 和 ETH 永远置顶
                def sort_key(data):
                    symbol = data['symbol']
                    # BTC 系列置顶（优先级最高）
                    if 'BTC' in symbol.upper():
                        return (0, -data['spread_value'])  # 0 = 最高优先级，按价差降序
                    # ETH 系列第二（优先级次高）
                    elif 'ETH' in symbol.upper():
                        return (1, -data['spread_value'])  # 1 = 次高优先级，按价差降序
                    # 其他代币按价差降序排列
                    else:
                        return (2, -data['spread_value'])  # 2 = 普通优先级

                symbol_data_list.sort(key=sort_key)
                
                # 更新缓存
                self.sorted_symbols_cache = [data['symbol']
                                             for data in symbol_data_list]
                self.last_sort_time = current_time
                
                self.logger.info(
                    f"排序完成，共{len(self.sorted_symbols_cache)}个交易对，前5名: {', '.join(self.sorted_symbols_cache[:5])}")
            
            # 🔥 第3步：按缓存的排序顺序显示（数据是实时的）
            # 如果缓存为空，使用当前可用数据的顺序
            symbols_to_display = self.sorted_symbols_cache if self.sorted_symbols_cache else list(
                symbol_data_dict.keys())
            
            for symbol in symbols_to_display:
                # 从dict中获取该symbol的最新数据
                if symbol not in symbol_data_dict:
                    continue
                
                data = symbol_data_dict[symbol]
                symbol = data['symbol']
                orderbook_prices = data['orderbook_prices']  # 🔥 改为订单簿价格
                funding_rates = data['funding_rates']
                
                # 🔥 存储订单簿数据（用于后续计算同向）
                orderbook_values = []  # [{bid, ask, bid_size, ask_size} or None]
                funding_rate_values = []
                row = []  # 初始化row（不包含symbol，最后再添加）
                
                # 🔥 预先计算费率差，用于判断是否高亮显示
                has_high_rate_diff = False
                
                # 🔥 第一步：收集订单簿价格和资金费率数据
                for exchange in self.config['exchanges']:
                    orderbook = orderbook_prices.get(exchange)
                    if orderbook:
                        orderbook_values.append(orderbook)  # {bid, ask, bid_size, ask_size}
                    else:
                        orderbook_values.append(None)
                    
                    if self.config['display'].get('show_funding_rates', True):
                        funding_rate = funding_rates.get(exchange)
                        funding_rate_values.append(funding_rate)
                
                # 🔥 第二步：预先计算同向和做多/做空交易所
                same_direction = False
                price_long_idx = None
                price_short_idx = None
                
                if (len(self.config['exchanges']) >= 2 and 
                    len([ob for ob in orderbook_values if ob is not None]) >= 2 and
                    len([fr for fr in funding_rate_values if fr is not None]) >= 2):
                    
                    # 1. 🔥 价差方向：使用中间价（bid+ask）/2来判断做多做空方向
                    valid_mid_prices = []
                    for i, ob in enumerate(orderbook_values):
                        if ob is not None:
                            mid_price = (ob["bid"] + ob["ask"]) / Decimal("2")
                            valid_mid_prices.append((i, mid_price))
                    
                    if len(valid_mid_prices) >= 2:
                        min_price_tuple = min(valid_mid_prices, key=lambda x: x[1])
                        max_price_tuple = max(valid_mid_prices, key=lambda x: x[1])
                        price_long_idx = min_price_tuple[0]  # 价格低的做多
                        price_short_idx = max_price_tuple[0]  # 价格高的做空
                        price_long_ex = self.config['exchanges'][price_long_idx]
                        
                        # 2. 资金费率方向：费率低（数学上小）的做多
                        valid_frs = [(i, fr) for i, fr in enumerate(
                            funding_rate_values) if fr is not None]
                        if len(valid_frs) >= 2:
                            min_fr_tuple = min(valid_frs, key=lambda x: x[1])
                            fr_long_ex = self.config['exchanges'][min_fr_tuple[0]]
                            
                            # 3. 判断是否同向
                            if price_long_ex == fr_long_ex:
                                same_direction = True
                
                # 🔥 第三步：构建row，显示买1/卖1价格，根据同向应用颜色
                for idx, exchange in enumerate(self.config['exchanges']):
                    orderbook = orderbook_values[idx] if idx < len(
                        orderbook_values) else None
                    
                    if orderbook is not None:
                        # 🔥 动态精度：根据价格大小决定显示位数
                        bid_price = float(orderbook["bid"])
                        ask_price = float(orderbook["ask"])
                        bid_size = float(orderbook["bid_size"])
                        ask_size = float(orderbook["ask_size"])
                        
                        precision = self._get_price_precision(bid_price)
                        
                        # 🔥 格式化买卖价和数量
                        bid_str = f"{bid_price:,.{precision}f}({bid_size:.2f})"
                        ask_str = f"{ask_price:,.{precision}f}({ask_size:.2f})"
                        price_str = f"{bid_str}/{ask_str}"
                        
                        # 🔥 根据同向判断应用颜色
                        if same_direction:
                            if idx == price_long_idx:
                                # 做多 = 绿色
                                price_str = f"[green]{price_str}[/green]"
                            elif idx == price_short_idx:
                                # 做空 = 红色
                                price_str = f"[red]{price_str}[/red]"
                        
                        row.append(price_str)
                    else:
                        row.append("-")
                    
                    # 添加资金费率（8小时 + 年化）
                    if self.config['display'].get('show_funding_rates', True):
                        funding_rate = funding_rate_values[idx] if idx < len(
                            funding_rate_values) else None
                        if funding_rate is not None:
                            # 8小时费率
                            fr_8h = float(funding_rate * 100)
                            # 年化费率：8小时 × 3次/天 × 365天 = × 1095
                            fr_annual = fr_8h * 1095
                            row.append(f"{fr_8h:.4f}%/{fr_annual:.1f}%")
                        else:
                            row.append("-")
                
                # 🔥 第四步：计算价差（只显示有利可图的价差）
                # 使用订单簿买1/卖1价格，只计算正向套利机会
                max_profitable_spread = Decimal("0")
                
                if len([ob for ob in orderbook_values if ob is not None]) >= 2:
                    # 尝试所有交易所两两组合，找到最大正价差
                    from itertools import combinations as combo
                    valid_orderbooks = [(i, ob) for i, ob in enumerate(orderbook_values) if ob is not None]
                    
                    for (idx1, ob1), (idx2, ob2) in combo(valid_orderbooks, 2):
                        # 正向套利1：在交易所1买入（ask1），在交易所2卖出（bid2）
                        if ob2["bid"] > ob1["ask"]:
                            spread = ((ob2["bid"] - ob1["ask"]) / ob1["ask"]) * Decimal("100")
                            max_profitable_spread = max(max_profitable_spread, spread)
                        
                        # 正向套利2：在交易所2买入（ask2），在交易所1卖出（bid1）
                        if ob1["bid"] > ob2["ask"]:
                            spread = ((ob1["bid"] - ob2["ask"]) / ob2["ask"]) * Decimal("100")
                            max_profitable_spread = max(max_profitable_spread, spread)
                
                # 只显示有利可图的价差（>0）
                if max_profitable_spread > 0:
                    row.append(f"{float(max_profitable_spread):.3f}%")
                else:
                    row.append("-")
                
                # 🔥 第五步：费率差计算（保留正负号，显示8小时 + 年化）
                if self.config['display'].get('show_funding_rates', True) and len(self.config['exchanges']) >= 2:
                    valid_fr_values = [
                        fr for fr in funding_rate_values if fr is not None]
                    if len(valid_fr_values) >= 2 and len(funding_rate_values) >= 2:
                        fr1 = funding_rate_values[0]  # EdgeX (已转换为8小时)
                        fr2 = funding_rate_values[1]  # Lighter (8小时)
                        
                        if fr1 is not None and fr2 is not None:
                            # 直接相减，保留正负号
                            # 正数：EdgeX费率更高（EdgeX空头收费，Lighter空头付费）
                            # 负数：Lighter费率更高（Lighter空头收费，EdgeX空头付费）
                            rate_diff = fr1 - fr2
                            
                            # 8小时差值
                            diff_8h = float(rate_diff * 100)
                            # 年化差值：8小时差值 × 1095
                            diff_annual = diff_8h * 1095
                            
                            # 🔥 判断是否有高费率差（年化≥50%）
                            if abs(diff_annual) >= self.rate_diff_threshold:
                                has_high_rate_diff = True
                            
                            # 🔥 更新费率差异跟踪
                            self._update_rate_diff_tracking(
                                symbol, diff_annual)
                            
                            # 显示时保留符号
                            sign = "+" if rate_diff >= 0 else ""
                            row.append(
                                f"{sign}{diff_8h:.4f}%/{sign}{diff_annual:.1f}%")
                            
                            # 🔥 添加持续时间显示
                            duration_str = self._get_rate_diff_duration(symbol)
                            row.append(duration_str)
                            
                            # 🔥 添加同向显示（已在前面计算）
                            row.append("是" if same_direction else "")
                        else:
                            row.append("-")
                            row.append("-")  # 持续时间列
                            row.append("")   # 同向列
                    else:
                        row.append("-")
                        row.append("-")  # 持续时间列
                        row.append("")   # 同向列
                
                # 🔥 构建完整的row，交易对名称根据费率差高亮
                symbol_display = f"[bold green]{symbol}[/bold green]" if has_high_rate_diff else symbol
                final_row = [symbol_display] + row
                
                price_table.add_row(*final_row)
            
            # 🔥 使用Layout布局管理（Header + Main + Logs + Controls）
            layout.split_column(
                Layout(self.create_header(), size=3),
                Layout(name="main"),
                Layout(self.create_logs_table(), size=23),  # 固定高度
                Layout(self.create_controls_panel(), size=3)
            )
            
            # 主内容区分为两个部分：统计 + 价格表（移除套利机会表格）
            layout["main"].split_column(
                Layout(Panel.fit(Text.assemble(
                    stats_text, "\n\n"), title="📊 统计"), size=5),
                Layout(price_table, name="prices")
            )
            
            return layout
        
        # 🔥 使用Layout布局管理（没有价格表的情况）
        layout.split_column(
            Layout(self.create_header(), size=3),
            Layout(name="main"),
            Layout(self.create_logs_table(), size=23),  # 固定高度
            Layout(self.create_controls_panel(), size=3)
        )
        
        # 主内容区只显示统计（移除套利机会表格）
        layout["main"].update(
            Panel.fit(Text.assemble(stats_text, "\n\n"), title="📊 统计")
        )
        
        return layout
    
    async def run_ui(self):
        """运行UI（使用全屏Layout模式，稳定无闪烁）"""
        self.running = True
        
        # 🔥 在UI启动前禁用控制台日志输出
        self._disable_console_logging()
        
        # 🔥 使用Rich Live全屏模式
        with Live(
            self.generate_display(),
            console=self.console,
            refresh_per_second=4,  # 每秒刷新4次，确保实时更新
            screen=True,  # 全屏模式，稳定布局
            transient=False
        ) as live:
            while self.running:
                try:
                    # 生成新的显示内容（获取最新数据）
                    layout = self.generate_display()
                    
                    # 🚀 更新显示（Layout自动管理布局，无闪烁）
                    live.update(layout)
                    
                    # 🚀 降低刷新频率到0.2秒（5Hz），避免阻塞事件循环
                    await asyncio.sleep(0.2)
                    
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    # 错误也记录到UI日志队列
                    self.logger.error(f"UI错误: {e}")
                    await asyncio.sleep(1)  # 避免错误循环
    
    async def run(self):
        """运行应用"""
        try:
            self.load_config()
            await self.initialize()
            
            # 🔥 不再使用console.print，直接启动UI
            # 所有日志都会显示在UI的日志表格中
            self.logger.info("套利监控系统已启动")
            
            await self.run_ui()
            
        except KeyboardInterrupt:
            self.logger.info("用户中断")
        except Exception as e:
            self.logger.error(f"系统错误: {e}", exc_info=True)
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        """清理资源"""
        self.logger.info("🧹 清理资源...")
        
        if self.monitor_service:
            await self.monitor_service.stop()
        
        for adapter in self.adapters.values():
            try:
                if hasattr(adapter, 'disconnect'):
                    await adapter.disconnect()
            except Exception as e:
                self.logger.error(f"❌ 断开连接失败: {e}")
        
        self.logger.info("✅ 资源清理完成")


async def main():
    """主函数"""
    app = ArbitrageMonitorApp()
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 程序退出")
    except Exception as e:
        print(f"❌ 程序异常: {e}")
        sys.exit(1)
