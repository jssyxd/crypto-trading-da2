"""
UI组件模块

职责：
- 提供可复用的UI组件
- 表格、面板、文本等Rich组件
- 支持动态精度和资金费率显示（参考 simple_printer.py）
"""

import json
from pathlib import Path
from typing import List, Dict, Optional
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout
from datetime import datetime, timedelta

from ..analysis.opportunity_finder import ArbitrageOpportunity


class UIComponents:
    """
    UI组件工厂
    
    支持动态精度和资金费率显示（参考 simple_printer.py）
    """
    
    # 🔥 类级别的精度配置缓存（参考 simple_printer.py）
    _market_precisions: Dict[str, Dict[str, int]] = {}
    _precision_loaded: bool = False
    
    @classmethod
    def _load_market_precisions(cls):
        """从配置文件加载市场精度信息（参考 simple_printer.py）"""
        if cls._precision_loaded:
            return
        
        try:
            # 尝试多个可能的配置文件路径
            config_paths = [
                Path("config/exchanges/lighter_markets.json"),  # 从当前工作目录（项目根目录）
                Path(__file__).parent.parent.parent.parent.parent / "config" / "exchanges" / "lighter_markets.json",  # 从文件位置向上5级
            ]
            
            config_path = None
            for path in config_paths:
                if path.exists():
                    config_path = path
                    break
            
            if config_path and config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    
                    markets = config_data.get('markets', {})
                    for symbol, market_info in markets.items():
                        # 提取精度信息
                        price_decimals = market_info.get('price_decimals')
                        size_decimals = market_info.get('size_decimals')
                        
                        if price_decimals is not None and size_decimals is not None:
                            cls._market_precisions[symbol] = {
                                'price_decimals': int(price_decimals),
                                'size_decimals': int(size_decimals)
                            }
                    
                    # 🔥 UI模式下不打印，避免界面闪动（静默加载）
                    pass
            
            cls._precision_loaded = True
        except Exception as e:
            # 🔥 UI模式下不打印，避免界面闪动（静默失败，使用默认精度）
            cls._precision_loaded = True  # 标记为已加载，避免重复尝试
    
    @classmethod
    def _get_precision(cls, symbol: str) -> Dict[str, int]:
        """
        获取交易对的精度信息（参考 simple_printer.py）
        
        Args:
            symbol: 交易对符号（如 "BTC-USDC-PERP"）
            
        Returns:
            {'price_decimals': int, 'size_decimals': int}
        """
        cls._load_market_precisions()
        
        # 提取基础币种（如 "BTC-USDC-PERP" -> "BTC"）
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0]
        
        if base_symbol in cls._market_precisions:
            return cls._market_precisions[base_symbol]
        else:
            # 默认精度（向后兼容）
            return {'price_decimals': 2, 'size_decimals': 1}
    
    @staticmethod
    def _format_size(size: float, size_decimals: int) -> str:
        """
        格式化数量（根据精度，参考 simple_printer.py）
        
        Args:
            size: 数量
            size_decimals: 数量精度（小数位数）
            
        Returns:
            格式化后的字符串
        """
        if size_decimals == 0:
            # 整数格式
            return f"{size:,.0f}"
        else:
            # 小数格式
            return f"{size:,.{size_decimals}f}"
    
    @staticmethod
    def _format_funding_rate(funding_rate: Optional[float]) -> str:
        """
        格式化资金费率显示（参考 simple_printer.py）
        
        Args:
            funding_rate: 资金费率（8小时）
            
        Returns:
            格式化后的字符串，格式：8h%/年化%
        """
        if funding_rate is None:
            return "-"
        fr_8h = float(funding_rate * 100)
        fr_annual = fr_8h * 1095
        return f"{fr_8h:.4f}%/{fr_annual:.1f}%"
    
    @staticmethod
    def create_summary_panel(stats: Dict) -> Panel:
        """
        创建摘要面板
        
        Args:
            stats: 统计数据
            
        Returns:
            摘要面板
        """
        text = Text()
        is_v3 = stats.get('is_v3_mode', False)
        if is_v3:
            text.append("🚀 套利执行终端 V3\n", style="bold white")
            text.append("（分段网格 / 执行控制台）\n", style="bright_black")
        else:
            text.append("🔍 套利监控系统 V2\n", style="bold white")  # 🔥 去掉空行
        
        # 🔥 运行模式（监控模式/实盘模式）
        monitor_only = stats.get('monitor_only_mode', True)
        if is_v3:
            if monitor_only:
                text.append("运行模式: 🔍 监控模式（不执行真实订单）\n", style="bold yellow")
            else:
                text.append("运行模式: ⚡ 实盘模式（执行真实订单）\n", style="bold green")
        else:
            text.append("运行模式: 🔎 纯监控（仅数据展示）\n", style="cyan")
        
        # 运行时间
        uptime = stats.get('uptime_seconds', 0)
        text.append(f"运行时间: {UIComponents._format_duration(uptime)}\n", style="cyan")
        
        # 交易所数量
        exchanges = stats.get('exchanges', [])
        text.append(f"交易所: {', '.join(exchanges)}\n", style="cyan")
        
        # 监控代币数量
        symbols_count = stats.get('symbols_count', 0)
        text.append(f"监控代币: {symbols_count} 个\n", style="cyan")
        
        # 活跃机会
        active_opps = stats.get('active_opportunities', 0)
        text.append(f"💰 活跃套利机会: {active_opps} 个\n", style="bold yellow")  # 🔥 去掉前面的空行
        
        # 🔥 WS重连次数统计
        reconnect_stats = stats.get('reconnect_stats', {})
        if reconnect_stats:
            reconnect_info = []
            for exchange, count in reconnect_stats.items():
                if count > 0:
                    reconnect_info.append(f"{exchange.upper()}={count}")
            if reconnect_info:
                text.append(f"🔄 WS重连: {', '.join(reconnect_info)}\n", style="yellow")
            else:
                text.append(f"🔄 WS重连: 无\n", style="green")
        else:
            text.append(f"🔄 WS重连: 无\n", style="green")
        
        return Panel(text, title="[bold white]系统状态[/bold white]", border_style="white")
    
    @staticmethod
    def create_risk_control_panel(risk_data: Dict) -> Panel:
        """
        创建风险控制面板
        
        Args:
            risk_data: 风险控制状态数据字典
            
        Returns:
            风险控制面板
        """
        text = Text()
        
        # 获取风险状态
        is_paused = risk_data.get('is_paused', False)
        pause_reason = risk_data.get('pause_reason', None)
        network_failure = risk_data.get('network_failure', False)
        exchange_maintenance = risk_data.get('exchange_maintenance', set())
        low_balance_exchanges = risk_data.get('low_balance_exchanges', set())
        critical_balance_exchanges = risk_data.get('critical_balance_exchanges', set())
        daily_trade_count = risk_data.get('daily_trade_count', 0)
        daily_trade_limit = risk_data.get('daily_trade_limit', 0)
        
        # 🔥 风险控制总体状态
        if is_paused:
            text.append("🚨 状态: ", style="bold cyan")
            text.append("已暂停", style="bold red")
            text.append(f" ({pause_reason})\n", style="yellow")
        else:
            text.append("✅ 状态: ", style="bold cyan")
            text.append("正常运行\n", style="bold green")
        
        # 🔥 网络状态
        if network_failure:
            text.append("🌐 网络: ", style="bold cyan")
            text.append("故障\n", style="bold red")
        else:
            text.append("🌐 网络: ", style="bold cyan")
            text.append("正常\n", style="green")
        
        # 🔥 交易所维护状态
        if exchange_maintenance:
            text.append("🔧 维护: ", style="bold cyan")
            text.append(f"{', '.join(exchange_maintenance)}\n", style="yellow")
        else:
            text.append("🔧 维护: ", style="bold cyan")
            text.append("无\n", style="green")
        
        # 🔥 余额状态
        if critical_balance_exchanges:
            text.append("💰 余额: ", style="bold cyan")
            text.append(f"严重不足 ({', '.join(critical_balance_exchanges)})\n", style="bold red")
        elif low_balance_exchanges:
            text.append("💰 余额: ", style="bold cyan")
            text.append(f"不足 ({', '.join(low_balance_exchanges)})\n", style="yellow")
        else:
            text.append("💰 余额: ", style="bold cyan")
            text.append("充足\n", style="green")

        # 🔥 错误避让状态
        backoff_exchanges = risk_data.get('backoff_exchanges', {})
        if backoff_exchanges:
            text.append("⏸️  错误避让: ", style="bold cyan")
            backoff_list = []
            for exchange, reason in backoff_exchanges.items():
                backoff_list.append(f"{exchange.upper()}({reason})")
            text.append(f"{', '.join(backoff_list)}\n", style="bold yellow")
        else:
            text.append("⏸️  错误避让: ", style="bold cyan")
            text.append("无\n", style="green")
        
        # 🔥 余额明细
        balance_summary = risk_data.get('exchange_balance_summary')
        if balance_summary:
            text.append("🏦 余额明细: ", style="bold cyan")
            text.append(f"{balance_summary}\n", style="white")
        
        # 🔥 每日交易次数
        if daily_trade_limit > 0:
            trade_percentage = (daily_trade_count / daily_trade_limit) * 100
            text.append("📊 今日交易: ", style="bold cyan")
            text.append(f"{daily_trade_count}/{daily_trade_limit} ", style="cyan")
            
            if trade_percentage >= 90:
                text.append(f"({trade_percentage:.0f}%)", style="bold red")
            elif trade_percentage >= 70:
                text.append(f"({trade_percentage:.0f}%)", style="yellow")
            else:
                text.append(f"({trade_percentage:.0f}%)", style="green")
            text.append("\n")
        
        return Panel(text, title="[bold white]🛡️ 全局风险控制[/bold white]", border_style="white")
    
    @classmethod
    def create_opportunities_table(
        cls, 
        opportunities: List[ArbitrageOpportunity], 
        limit: int = 20,
        ui_opportunity_tracking: Optional[Dict[str, Dict]] = None,
        symbol_occurrence_timestamps: Optional[Dict[str, List]] = None
    ) -> Panel:
        """
        创建机会表格（使用Panel样式，不显示格子边框）
        
        Args:
            opportunities: 机会列表
            limit: 显示数量限制
            ui_opportunity_tracking: UI层持续时间跟踪 {key: {'ui_duration_start': datetime, 'last_seen': datetime}}
            symbol_occurrence_timestamps: 代币出现时间戳 {symbol: [timestamp1, timestamp2, ...]}
            
        Returns:
            机会面板
        """
        text = Text()
        text.append(f"🏆 套利机会 Top {min(len(opportunities), limit)}\n\n", style="bold white")
        
        if opportunities:
            # 表头（增加触发条件列）
            # 定义列宽度常量，确保表头和数据行一致
            COL_WIDTH_TOKEN = 10
            COL_WIDTH_BUY_EX = 12
            COL_WIDTH_SELL_EX = 12
            COL_WIDTH_PRICE = 35
            COL_WIDTH_SPREAD = 10
            COL_WIDTH_FR_DIFF = 20
            COL_WIDTH_TRIGGER = 18  # 🔥 触发条件列宽度
            COL_WIDTH_DURATION = 12
            COL_WIDTH_OCCURRENCE = 10  # 🔥 出现次数列宽度
            COL_WIDTH_SAME_DIR = 6  # 🔥 同向列宽度
            
            # 🔥 表头（确保与数据行对齐，列之间只有一个空格）
            header = (
                f"{'代币':<{COL_WIDTH_TOKEN}} "
                f"{'买入交易所':<{COL_WIDTH_BUY_EX}} "
                f"{'卖出交易所':<{COL_WIDTH_SELL_EX}} "
                f"{'买价/卖价':<{COL_WIDTH_PRICE}} "
                f"{'价差%':>{COL_WIDTH_SPREAD}} "  # 右对齐，后面有空格
                f"{'费率差(年化)':>{COL_WIDTH_FR_DIFF}} "  # 右对齐，后面有空格
                f"{'触发条件':<{COL_WIDTH_TRIGGER}} "  # 🔥 新增触发条件列
                f"{'持续时间':<{COL_WIDTH_DURATION}} "  # 左对齐，后面有空格
                f"{'出现次数':>{COL_WIDTH_OCCURRENCE}} "  # 右对齐，后面有空格
                f"{'同向':<{COL_WIDTH_SAME_DIR}}\n"  # 左对齐
            )
            text.append(header, style="dim white")
            text.append("─" * 120 + "\n", style="dim white")
            
            # 数据行（使用相同的列宽度，确保对齐）
            for opp in opportunities[:limit]:
                # 🔥 获取精度信息（动态精度，与实时订单簿表格一致）
                precision = cls._get_precision(opp.symbol)
                price_decimals = precision['price_decimals']
                size_decimals = precision['size_decimals']
                
                # 🔥 使用动态精度格式化价格和数量
                price_buy_str = f"{opp.price_buy:,.{price_decimals}f}"
                size_buy_str = cls._format_size(opp.size_buy, size_decimals)
                price_sell_str = f"{opp.price_sell:,.{price_decimals}f}"
                size_sell_str = cls._format_size(opp.size_sell, size_decimals)
                
                price_str = f"{price_buy_str}({size_buy_str}) / {price_sell_str}({size_sell_str})"
                
                # 🔥 UI层持续时间（带2秒容差）
                ui_duration_seconds = 0.0
                if ui_opportunity_tracking:
                    key = opp.get_opportunity_key()
                    if key in ui_opportunity_tracking:
                        tracking = ui_opportunity_tracking[key]
                        current_time = datetime.now()
                        ui_duration_seconds = (current_time - tracking['ui_duration_start']).total_seconds()
                duration_str = UIComponents._format_duration(ui_duration_seconds)
                
                # 🔥 出现次数（过去15分钟）
                occurrence_count = 0
                if symbol_occurrence_timestamps and opp.symbol in symbol_occurrence_timestamps:
                    cutoff_time = datetime.now() - timedelta(minutes=15)
                    occurrence_count = len([
                        ts for ts in symbol_occurrence_timestamps[opp.symbol]
                        if ts > cutoff_time
                    ])
                occurrence_str = f"{occurrence_count}"
                
                # 🔥 格式化资金费率差（年化百分比，已从orchestrator计算好）
                diff_annual = 0  # 🔥 初始化，用于样式判断
                if opp.funding_rate_diff is not None:
                    # 🔥 opp.funding_rate_diff 已经是年化费率差百分比（来自 funding_rate_diff_annual）
                    # 直接使用，不需要再次转换
                    diff_annual = abs(opp.funding_rate_diff)  # 确保是正数
                    # 费率差永远是正数，不需要符号，右对齐，固定宽度
                    funding_rate_diff_str = f"{diff_annual:.1f}%".rjust(COL_WIDTH_FR_DIFF)
                else:
                    funding_rate_diff_str = "-".rjust(COL_WIDTH_FR_DIFF)  # 🔥 右对齐
                
                # 🔥 格式化触发条件（显示套利模式和具体条件）
                trigger_str = ""
                if opp.trigger_mode and opp.trigger_condition:
                    # 将 mode 转换为中文描述
                    mode_cn = {
                        "spread": "价差套利",
                        "funding_rate": "资金费率套利"
                    }.get(opp.trigger_mode, opp.trigger_mode)
                    trigger_str = f"{mode_cn}-{opp.trigger_condition}"
                trigger_str = trigger_str.ljust(COL_WIDTH_TRIGGER)
                
                # 🔥 使用相同的列宽度格式化，确保对齐（与表头对齐方式一致）
                # 价差%：右对齐，固定宽度
                spread_pct_str = f"{opp.spread_pct:>+{COL_WIDTH_SPREAD-1}.3f}%".rjust(COL_WIDTH_SPREAD)
                
                # 🔥 构建行内容，所有数据默认使用灰色（dim white），只有资金费率差在达到阈值时使用白色
                # 确保列之间只有一个空格，与表头一致
                row_prefix = (
                    f"{opp.symbol:<{COL_WIDTH_TOKEN}} "
                    f"{opp.exchange_buy:<{COL_WIDTH_BUY_EX}} "
                    f"{opp.exchange_sell:<{COL_WIDTH_SELL_EX}} "
                    f"{price_str:<{COL_WIDTH_PRICE}} "
                    f"{spread_pct_str} "  # 🔥 右对齐，后面有空格（与表头一致）
                )
                
                # 🔥 资金费率差样式：>=40时使用白色，否则使用dim white
                if funding_rate_diff_str.strip() != "-" and diff_annual >= 40:
                    funding_rate_diff_style = "white"
                else:
                    funding_rate_diff_style = "dim white"
                
                # 🔥 计算同向（参考实时订单簿表格的算法）
                # 1. 价差方向：买入交易所做多（因为买入交易所价格低），卖出交易所做空（因为卖出交易所价格高）
                # 2. 资金费率方向：费率低的交易所做多
                # 3. 判断是否同向：如果买入交易所的资金费率 <= 卖出交易所的资金费率，则同向
                same_direction_str = ""
                if opp.funding_rate_buy is not None and opp.funding_rate_sell is not None:
                    # 买入交易所做多，如果买入交易所的资金费率 <= 卖出交易所的资金费率，则同向
                    if opp.funding_rate_buy <= opp.funding_rate_sell:
                        same_direction_str = "同向"
                
                # 🔥 确保出现次数右对齐（与表头一致）
                occurrence_str_formatted = f"{occurrence_str:>{COL_WIDTH_OCCURRENCE}}"
                
                row_suffix = (
                    f"{trigger_str} "  # 🔥 触发条件，左对齐，后面有空格
                    f"{duration_str:<{COL_WIDTH_DURATION}} "  # 🔥 左对齐，后面有空格（与表头一致）
                    f"{occurrence_str_formatted} "  # 🔥 右对齐，后面有空格（与表头一致）
                    f"{same_direction_str:<{COL_WIDTH_SAME_DIR}}\n"  # 🔥 左对齐（与表头一致）
                )
                
                # 🔥 使用Rich的Text对象分段设置样式（直接在text上append，确保样式正确应用）
                # 所有数据默认使用灰色（bright_black），只有资金费率差在达到阈值时使用白色
                # 使用bright_black确保灰色更明显，避免dim white在某些终端中看起来像白色
                text.append(row_prefix, style="bright_black")
                text.append(funding_rate_diff_str + " ", style=funding_rate_diff_style)  # 🔥 资金费率差使用单独样式
                text.append(row_suffix, style="bright_black")
        else:
            text.append("暂无套利机会\n", style="dim white")
        
        return Panel(text, title="[bold white]套利机会[/bold white]", border_style="white")
    
    @classmethod
    def create_price_table(
        cls, 
        orderbook_data: Dict, 
        symbols: List[str], 
        exchanges: List[str], 
        ticker_data: Optional[Dict] = None,
        symbol_spreads: Optional[Dict[str, List]] = None  # 🔥 后台计算的价差数据 {symbol: [SpreadData, ...]}，包含所有方向的价差
    ) -> Panel:
        """
        创建实时价格表格（显示所有代币的订单簿数据，使用Table样式，类似Excel）
        
        Args:
            orderbook_data: 订单簿数据 {exchange: {symbol: OrderBookData}}
            symbols: 交易对列表
            exchanges: 交易所列表
            ticker_data: Ticker数据 {exchange: {symbol: TickerData}}，用于获取资金费率（可选）
            symbol_spreads: 后台计算的价差数据 {symbol: spread_pct}，用于保证数据一致性（可选）
            
        Returns:
            价格面板（包含Table）
        """
        # 🔥 创建Table（类似Excel样式，灰色边框和网格线）
        from rich.box import SQUARE  # 使用方形边框样式（类似Excel）
        table = Table(
            show_header=True,
            header_style="bold white",
            border_style="dim white",  # 🔥 灰色边框（与字体颜色一致的灰色）
            box=SQUARE,  # 🔥 使用方形边框样式（类似Excel，包含完整边框和网格线）
            show_lines=True,  # 🔥 显示内部网格线（行和列之间的分隔线）
            padding=(0, 1),  # 🔥 最小化单元格内边距（垂直0，水平1）以缩小表格高度
            collapse_padding=True  # 🔥 折叠内边距，进一步缩小高度
        )
        
        # 🔥 添加表头列（设置固定宽度，不换行，超出隐藏）
        # 使用固定宽度，no_wrap=True，overflow="ellipsis" 隐藏超出内容
        table.add_column("交易对", style="white", width=18, no_wrap=True, overflow="ellipsis")
        for exchange in exchanges:
            # 🔥 买1/卖1列需要更多空间显示价格和数量（增加宽度）
            table.add_column(f"{exchange.upper()} 买1/卖1", style="white", width=42, no_wrap=True, overflow="ellipsis")
            # 🔥 费率列需要显示费率百分比
            table.add_column(f"{exchange.upper()} 费率", style="white", width=18, no_wrap=True, overflow="ellipsis")
        # 🔥 价差列：简化后显示首字母+百分比，增加宽度以完整显示
        table.add_column("价差%", style="white", justify="right", width=30, no_wrap=True, overflow="ellipsis")
        table.add_column("费率差(年化)", style="white", justify="right", width=15, no_wrap=True, overflow="ellipsis")
        table.add_column("同向", style="white", justify="center", width=6, no_wrap=True, overflow="ellipsis")
        
        # 🔥 按字母排序交易对
        sorted_symbols = sorted(symbols)
        
        # 遍历所有交易对（按字母顺序）
        for symbol in sorted_symbols:
            # 🔥 获取精度信息（动态精度）
            precision = cls._get_precision(symbol)
            price_decimals = precision['price_decimals']
            size_decimals = precision['size_decimals']
            
            # 🔥 收集所有交易所的数据
            exchange_data = {}
            for exchange in exchanges:
                if exchange in orderbook_data and symbol in orderbook_data[exchange]:
                    ob = orderbook_data[exchange][symbol]
                    if ob and ob.best_bid and ob.best_ask:
                        # 获取资金费率
                        funding_rate = None
                        if ticker_data and exchange in ticker_data and symbol in ticker_data[exchange]:
                            ticker = ticker_data[exchange][symbol]
                            if hasattr(ticker, 'funding_rate') and ticker.funding_rate is not None:
                                funding_rate = float(ticker.funding_rate)
                        
                        exchange_data[exchange] = {
                            'bid_price': float(ob.best_bid.price),
                            'bid_size': float(ob.best_bid.size),
                            'ask_price': float(ob.best_ask.price),
                            'ask_size': float(ob.best_ask.size),
                            'funding_rate': funding_rate
                        }
            
            # 🔥 构建数据行
            row_cells = [symbol]  # 交易对列
            
            for exchange in exchanges:
                if exchange in exchange_data:
                    ex_data = exchange_data[exchange]
                    # 格式化价格和数量（使用动态精度，单行显示）
                    bid_price_str = f"${ex_data['bid_price']:>8,.{price_decimals}f}"
                    bid_size_str = cls._format_size(ex_data['bid_size'], size_decimals)
                    ask_price_str = f"${ex_data['ask_price']:>8,.{price_decimals}f}"
                    ask_size_str = cls._format_size(ex_data['ask_size'], size_decimals)
                    
                    # 🔥 单行显示：买1×数量 / 卖1×数量
                    price_str = f"{bid_price_str}×{bid_size_str} / {ask_price_str}×{ask_size_str}"
                    row_cells.append(price_str)
                    
                    # 格式化资金费率
                    fr_str = cls._format_funding_rate(ex_data['funding_rate'])
                    row_cells.append(fr_str)
                else:
                    row_cells.append("—")
                    row_cells.append("—")
            
            # 🔥 计算价差（优先使用后台计算的数据，保证数据一致性）
            # 🔥 方案A：只显示最优开仓方向 + 对应的平仓价差
            spread_str = "—"
            spread_style = "white"
            
            # 🔥 优先使用后台计算的价差数据（与套利机会表格一致）
            if symbol_spreads and symbol in symbol_spreads:
                spreads_list = symbol_spreads[symbol]
                if isinstance(spreads_list, list) and len(spreads_list) > 0:
                    # 🔥 找到最优的开仓方向（价差最大的）
                    best_opening_spread = None
                    max_spread_pct = -float('inf')
                    
                    for spread_data in spreads_list:
                        if hasattr(spread_data, 'spread_pct'):
                            spread_pct = spread_data.spread_pct
                        elif isinstance(spread_data, dict):
                            spread_pct = spread_data.get('spread_pct', 0)
                        else:
                            continue
                        
                        if spread_pct > max_spread_pct:
                            max_spread_pct = spread_pct
                            best_opening_spread = spread_data
                    
                    if best_opening_spread:
                        # 获取开仓方向信息
                        if hasattr(best_opening_spread, 'spread_pct'):
                            opening_pct = best_opening_spread.spread_pct
                            exchange_buy = best_opening_spread.exchange_buy
                            exchange_sell = best_opening_spread.exchange_sell
                        else:
                            opening_pct = best_opening_spread.get('spread_pct', 0)
                            exchange_buy = best_opening_spread.get('exchange_buy', '')
                            exchange_sell = best_opening_spread.get('exchange_sell', '')
                        
                        # 🔥 计算平仓价差（反向）
                        # 在 spreads_list 中查找反向的价差
                        closing_pct = None
                        for spread_data in spreads_list:
                            if hasattr(spread_data, 'exchange_buy'):
                                check_buy = spread_data.exchange_buy
                                check_sell = spread_data.exchange_sell
                            elif isinstance(spread_data, dict):
                                check_buy = spread_data.get('exchange_buy', '')
                                check_sell = spread_data.get('exchange_sell', '')
                            else:
                                continue
                            
                            # 找到反向的价差（买卖交换）
                            if check_buy == exchange_sell and check_sell == exchange_buy:
                                if hasattr(spread_data, 'spread_pct'):
                                    closing_pct = spread_data.spread_pct
                                else:
                                    closing_pct = spread_data.get('spread_pct', 0)
                                break
                        
                        # 🔥 获取交易所首字母（大写）
                        buy_initial = exchange_buy[0].upper() if exchange_buy else ''
                        sell_initial = exchange_sell[0].upper() if exchange_sell else ''
                        
                        # 🔥 格式化显示：L→E: +0.123% / 平:-0.026%
                        opening_str = f"{buy_initial}→{sell_initial}: {opening_pct:+.3f}%"
                        if closing_pct is not None:
                            closing_str = f"平:{closing_pct:+.3f}%"
                            spread_str = f"{opening_str} / {closing_str}"
                        else:
                            spread_str = opening_str
                        
                        # 根据开仓价差设置样式
                        if opening_pct >= 0.5:
                            spread_style = "bold white"
                        elif opening_pct >= 0.2:
                            spread_style = "white"
                        else:
                            spread_style = "dim white"
                elif isinstance(spreads_list, (int, float)):
                    # 🔥 兼容旧格式（单个数值）
                    best_spread_pct = spreads_list
                    if best_spread_pct > 0:
                        spread_str = f"{best_spread_pct:+.3f}%"
                        if best_spread_pct >= 0.5:
                            spread_style = "bold white"
                        elif best_spread_pct >= 0.2:
                            spread_style = "white"
                        else:
                            spread_style = "dim white"
                    else:
                        spread_str = f"{best_spread_pct:.3f}%"
                        spread_style = "dim white"
            elif len(exchange_data) >= 2:
                # 如果没有后台计算的价差数据，则在前端计算（向后兼容）
                best_spread_pct = 0
                for ex1 in exchange_data:
                    for ex2 in exchange_data:
                        if ex1 != ex2:
                            ex1_data = exchange_data[ex1]
                            ex2_data = exchange_data[ex2]
                            # 策略：ex1买 -> ex2卖
                            profit = ex2_data['bid_price'] - ex1_data['ask_price']
                            profit_pct = (profit / ex1_data['ask_price']) * 100 if ex1_data['ask_price'] > 0 else 0
                            if profit_pct > best_spread_pct:
                                best_spread_pct = profit_pct
                
                if best_spread_pct > 0:
                    spread_str = f"{best_spread_pct:+.3f}%"
                    if best_spread_pct >= 0.5:
                        spread_style = "bold white"
                    elif best_spread_pct >= 0.2:
                        spread_style = "white"
                    else:
                        spread_style = "dim white"
                else:
                    spread_str = f"{best_spread_pct:.3f}%"
                    spread_style = "dim white"
            else:
                spread_str = "—"
                spread_style = "dim white"
            
            # 🔥 价差列也使用Text对象，确保样式正确应用
            from rich.text import Text
            spread_text = Text(spread_str, style=spread_style)
            row_cells.append(spread_text)
            
            # 🔥 计算资金费率差（参考v1算法：8小时费率差转换为年化费率差）
            funding_rate_diff_str = "—"
            max_diff_annual = 0  # 🔥 初始化，用于样式判断
            if len(exchange_data) >= 2:
                # 收集所有交易所的资金费率（8小时费率，小数形式）
                funding_rates = {}
                for ex in exchange_data:
                    if exchange_data[ex]['funding_rate'] is not None:
                        funding_rates[ex] = exchange_data[ex]['funding_rate']
                
                # 如果有2个或更多交易所的资金费率，计算费率差
                if len(funding_rates) >= 2:
                    # 计算所有交易所之间的费率差，取绝对值最大的
                    max_diff_annual = 0
                    for ex1 in funding_rates:
                        for ex2 in funding_rates:
                            if ex1 != ex2:
                                # 🔥 资金费率差应该永远为正数（绝对值差值）
                                rate_diff = abs(funding_rates[ex2] - funding_rates[ex1])
                                # 8小时差值（百分比）
                                diff_8h = float(rate_diff * 100)
                                # 年化差值：8小时差值 × 1095
                                diff_annual = diff_8h * 1095
                                # 取最大的费率差（已经是正数）
                                if diff_annual > max_diff_annual:
                                    max_diff_annual = diff_annual
                    
                    if max_diff_annual != 0:
                        # 费率差永远是正数，不需要符号
                        funding_rate_diff_str = f"{max_diff_annual:.1f}%"
            
            # 🔥 费率差样式：绝对值>=40时使用白色，否则使用dim white
            # 使用Rich的Text对象为费率差列单独设置样式
            if funding_rate_diff_str != "—":
                funding_rate_diff_style = "white" if max_diff_annual >= 40 else "dim white"
                funding_rate_diff_text = Text(funding_rate_diff_str, style=funding_rate_diff_style)
            else:
                funding_rate_diff_text = Text(funding_rate_diff_str, style="dim white")
            
            row_cells.append(funding_rate_diff_text)
            
            # 🔥 计算同向（参考v1算法）
            same_direction_str = "—"
            if len(exchange_data) >= 2:
                # 1. 价差方向：使用中间价（bid+ask）/2来判断做多做空方向
                mid_prices = {}
                for ex in exchange_data:
                    ex_data = exchange_data[ex]
                    mid_price = (ex_data['bid_price'] + ex_data['ask_price']) / 2.0
                    mid_prices[ex] = mid_price
                
                # 价格低的交易所做多，价格高的交易所做空
                if len(mid_prices) >= 2:
                    price_long_ex = min(mid_prices.items(), key=lambda x: x[1])[0]  # 价格低的做多
                    
                    # 2. 资金费率方向：费率低（数学上小）的做多
                    funding_rates_for_direction = {}
                    for ex in exchange_data:
                        if exchange_data[ex]['funding_rate'] is not None:
                            funding_rates_for_direction[ex] = exchange_data[ex]['funding_rate']
                    
                    if len(funding_rates_for_direction) >= 2:
                        fr_long_ex = min(funding_rates_for_direction.items(), key=lambda x: x[1])[0]  # 费率低的做多
                        
                        # 3. 判断是否同向：如果价差方向中做多的交易所和资金费率方向中做多的交易所是同一个，就是同向
                        if price_long_ex == fr_long_ex:
                            same_direction_str = "是"
                        else:
                            same_direction_str = ""
            
            row_cells.append(same_direction_str)
            
            # 🔥 添加数据行（不使用行样式，让每个单元格的Text对象自己管理样式）
            table.add_row(*row_cells)
        
        # 🔥 返回Panel包装的Table
        return Panel(table, title="[bold white]实时订单簿价格 + 资金费率[/bold white]", border_style="white")
    
    @staticmethod
    def create_performance_panel(stats: Dict) -> Panel:
        """
        创建性能面板
        
        Args:
            stats: 性能统计
            
        Returns:
            性能面板
        """
        text = Text()
        text.append("⚡ 性能指标\n", style="bold white")  # 🔥 保持标题单独一行，但去掉多余空行
        
        # 队列状态
        ob_q = stats.get('orderbook_queue_size', 0)
        ticker_q = stats.get('ticker_queue_size', 0)
        analysis_q = stats.get('analysis_queue_size', 0)
        
        text.append(f"队列积压: ", style="bold cyan")
        text.append(f"订单簿={ob_q} Ticker={ticker_q} 分析={analysis_q}\n", style="cyan")
        # 队列峰值（观测波动时的爆点）
        ob_peak = stats.get('orderbook_queue_peak', 0)
        tk_peak = stats.get('ticker_queue_peak', 0)
        text.append(f"队列峰值: ", style="bold cyan")
        text.append(f"订单簿={ob_peak} Ticker={tk_peak}\n", style="cyan")
        
        # 分析延迟
        latency = stats.get('analysis_latency_ms', 0)
        text.append(f"分析延迟: ", style="bold cyan")
        if latency > 100:
            text.append(f"{latency:.1f}ms\n", style="yellow")
        else:
            text.append(f"{latency:.1f}ms\n", style="green")
        
        # 处理量（去掉前面的空行）
        ob_processed = stats.get('orderbook_processed', 0)
        ticker_processed = stats.get('ticker_processed', 0)
        text.append(f"处理量: ", style="bold cyan")
        text.append(f"订单簿={ob_processed} Ticker={ticker_processed}\n", style="cyan")
        
        # 🎯 UI更新频率（抽样率）
        ui_update_interval = stats.get('ui_update_interval', 1.0)
        text.append(f"UI抽样: ", style="bold cyan")
        text.append(f"{ui_update_interval:.1f}秒/次\n", style="cyan")
        
        # 丢弃量
        ob_dropped = stats.get('orderbook_dropped', 0)
        ticker_dropped = stats.get('ticker_dropped', 0)
        if ob_dropped > 0 or ticker_dropped > 0:
            text.append(f"丢弃量: ", style="bold yellow")
            text.append(f"订单簿={ob_dropped} Ticker={ticker_dropped}\n", style="yellow")
        
        # 🔥 网络流量统计（去掉前面的空行）
        bytes_received = stats.get('network_bytes_received', 0)
        bytes_sent = stats.get('network_bytes_sent', 0)
        
        def format_bytes(bytes_count: int) -> str:
            """格式化字节数为可读格式"""
            if bytes_count == 0:
                return "0 B"
            elif bytes_count < 1024:
                return f"{bytes_count} B"
            elif bytes_count < 1024 * 1024:
                return f"{bytes_count / 1024:.2f} KB"
            elif bytes_count < 1024 * 1024 * 1024:
                return f"{bytes_count / (1024 * 1024):.2f} MB"
            else:
                return f"{bytes_count / (1024 * 1024 * 1024):.2f} GB"
        
        text.append(f"网络流量: ", style="bold cyan")
        text.append(f"接收={format_bytes(bytes_received)} 发送={format_bytes(bytes_sent)}\n", style="cyan")
        
        return Panel(text, title="[bold white]性能[/bold white]", border_style="white")
    
    @staticmethod
    def create_debug_panel(debug_messages: List[str]) -> Panel:
        """
        创建Debug面板
        
        Args:
            debug_messages: Debug消息列表
            
        Returns:
            Debug面板
        """
        text = Text()
        text.append("🐛 Debug 输出\n\n", style="bold yellow")
        
        for msg in debug_messages[-10:]:  # 只显示最近10条
            text.append(f"{msg}\n", style="dim")
        
        if not debug_messages:
            text.append("（无Debug消息）\n", style="dim")
        
        return Panel(text, title="[bold]Debug[/bold]", border_style="yellow")
    
    @staticmethod
    def _format_duration(seconds: float) -> str:
        """
        格式化持续时间
        
        Args:
            seconds: 秒数
            
        Returns:
            格式化字符串
        """
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds/60)}m{int(seconds%60)}s"
        else:
            hours = int(seconds / 3600)
            mins = int((seconds % 3600) / 60)
            return f"{hours}h{mins}m"

    @staticmethod
    def create_positions_table(positions: List[Dict]) -> Panel:
        """
        创建持仓信息表格（V3模式）
        
        Args:
            positions: 持仓信息列表
            
        Returns:
            持仓信息面板
        """
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("交易对", style="cyan", width=12)
        table.add_column("买入交易所", style="green", width=10)
        table.add_column("卖出交易所", style="red", width=10)
        table.add_column("买入数量", style="green", width=10, justify="right")
        table.add_column("卖出数量", style="red", width=10, justify="right")
        table.add_column("买入价格", style="green", width=12, justify="right")
        table.add_column("卖出价格", style="red", width=12, justify="right")
        table.add_column("开仓价差%", style="yellow", width=10, justify="right")
        table.add_column("开仓时间", style="dim white", width=11)
        table.add_column("模式", style="magenta", width=8)
        
        if not positions:
            table.add_row("暂无持仓", "", "", "", "", "", "", "", "", "")
        else:
            for pos in positions:
                symbol = pos.get('symbol', 'N/A')
                exchange_buy = pos.get('exchange_buy', 'N/A')
                exchange_sell = pos.get('exchange_sell', 'N/A')
                quantity_buy = pos.get('quantity_buy', 0)
                quantity_sell = pos.get('quantity_sell', 0)
                spread_pct = pos.get('open_spread_pct', 0.0)
                open_time = pos.get('open_time', 'N/A')
                mode = pos.get('open_mode', 'N/A')
                
                # 🔥 获取持仓价格（开仓时的价格）
                price_buy = pos.get('open_price_buy', 0.0)
                price_sell = pos.get('open_price_sell', 0.0)
                
                # 格式化数量（分别显示两个交易所的数量）
                quantity_buy_str = f"{float(quantity_buy):.4f}" if quantity_buy else "0.0000"
                quantity_sell_str = f"{float(quantity_sell):.4f}" if quantity_sell else "0.0000"
                
                # 🔥 格式化持仓价格
                price_buy_str = f"{float(price_buy):.2f}" if price_buy else "-"
                price_sell_str = f"{float(price_sell):.2f}" if price_sell else "-"
                
                # 🔥 检测持仓不一致
                qty_buy_float = float(quantity_buy) if quantity_buy else 0.0
                qty_sell_float = float(quantity_sell) if quantity_sell else 0.0
                is_mismatch = abs(qty_buy_float - qty_sell_float) > 0.0001
                
                # 如果持仓不一致，使用红色高亮
                if is_mismatch:
                    quantity_buy_str = f"[bold red]{quantity_buy_str}⚠[/bold red]"
                    quantity_sell_str = f"[bold red]{quantity_sell_str}⚠[/bold red]"
                
                # 格式化价差
                spread_str = f"{spread_pct:.3f}%" if spread_pct else "0.000%"
                
                # 🔥 格式化时间（只显示日期和时:分）
                if open_time != 'N/A' and len(open_time) > 16:
                    open_time_short = open_time[5:16]  # MM-DD HH:MM
                else:
                    open_time_short = open_time
                
                table.add_row(
                    symbol,
                    exchange_buy,
                    exchange_sell,
                    quantity_buy_str,
                    quantity_sell_str,
                    price_buy_str,
                    price_sell_str,
                    spread_str,
                    open_time_short,
                    mode
                )
        
        return Panel(table, title="[bold white]💰 持仓信息[/bold white]", border_style="green")
    
    @staticmethod
    def create_accounts_table(balances: Dict[str, List[Dict]]) -> Panel:
        """
        创建账户信息表格（V3模式）
        
        Args:
            balances: 账户余额字典 {exchange: [BalanceData, ...]}
            
        Returns:
            账户信息面板
        """
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("交易所", style="cyan", width=10)
        table.add_column("币种", style="white", width=6)
        table.add_column("可用", style="green", width=12)
        table.add_column("已用", style="yellow", width=12)
        table.add_column("总计", style="bold white", width=12)
        table.add_column("来源", style="magenta", width=6)  # 🔥 新增：显示ws/rest
        
        if not balances:
            table.add_row("暂无数据", "", "", "", "", "")
        else:
            for exchange, balance_list in balances.items():
                if not balance_list:
                    table.add_row(exchange, "N/A", "0.00", "0.00", "0.00", "-")
                else:
                    for i, balance in enumerate(balance_list):
                        currency = balance.get('currency', 'N/A')
                        free = balance.get('free', 0)
                        used = balance.get('used', 0)
                        total = balance.get('total', 0)
                        source = balance.get('source', 'rest')  # 🔥 获取来源：ws或rest
                        
                        # 格式化数值
                        free_str = f"{float(free):.2f}" if free else "0.00"
                        used_str = f"{float(used):.2f}" if used else "0.00"
                        total_str = f"{float(total):.2f}" if total else "0.00"
                        
                        # 格式化来源（ws用绿色，rest用黄色）
                        if source == 'ws':
                            source_style = "[green]ws[/green]"
                        else:
                            source_style = "[yellow]rest[/yellow]"
                        
                        # 第一行显示交易所名称，后续行留空
                        exchange_name = exchange if i == 0 else ""
                        table.add_row(
                            exchange_name,
                            currency,
                            free_str,
                            used_str,
                            total_str,
                            source_style
                        )
        
        return Panel(table, title="[bold white]💳 账户余额[/bold white]", border_style="blue")
    
    @staticmethod
    def create_execution_records_table(records: List[Dict]) -> Panel:
        """
        创建执行记录表格（V3模式）
        
        Args:
            records: 执行记录列表
            
        Returns:
            执行记录面板
        """
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("时间", style="dim white", width=10)
        table.add_column("交易对", style="cyan", width=10)
        table.add_column("类型", style="magenta", width=6)
        table.add_column("数量", style="yellow", width=10, justify="right")
        table.add_column("买入价格", style="green", width=12, justify="right")
        table.add_column("卖出价格", style="red", width=12, justify="right")
        table.add_column("真实价差%", style="cyan", width=12, justify="right")
        table.add_column("买入交易所", style="green", width=10)
        table.add_column("卖出交易所", style="red", width=10)
        table.add_column("状态", style="bold", width=6)
        table.add_column("错误", style="red", width=15)
        
        if not records:
            table.add_row("暂无执行记录", "", "", "", "", "", "", "", "", "", "")
        else:
            # 显示最近20条记录（倒序，最新的在前）
            for record in records[-20:][::-1]:
                exec_time = record.get('execution_time', 'N/A')
                symbol = record.get('symbol', 'N/A')
                is_open = record.get('is_open', True)
                exchange_buy = record.get('exchange_buy', 'N/A')
                exchange_sell = record.get('exchange_sell', 'N/A')
                success = record.get('success', False)
                error_msg = record.get('error_message', '')
                
                # 🔥 获取交易关键数据
                quantity = record.get('quantity', 0.0)
                price_buy = record.get('price_buy', 0.0)
                price_sell = record.get('price_sell', 0.0)
                actual_spread_pct = record.get('actual_spread_pct', None)
                
                # 格式化时间（只显示时:分:秒）
                from datetime import datetime as dt
                if isinstance(exec_time, dt):
                    time_str = exec_time.strftime('%H:%M:%S')
                else:
                    time_str = str(exec_time)
                
                # 类型
                type_str = "开仓" if is_open else "平仓"
                
                # 状态
                if success:
                    status_str = "[green]✓[/green]"
                else:
                    status_str = "[red]✗[/red]"
                
                # 错误信息（截断）
                error_str = error_msg[:13] + "..." if len(error_msg) > 13 else error_msg
                
                # 🔥 格式化交易数据
                quantity_str = f"{quantity:.4f}" if quantity > 0 else "-"
                price_buy_str = f"{price_buy:.2f}" if price_buy > 0 else "-"
                price_sell_str = f"{price_sell:.2f}" if price_sell > 0 else "-"
                if actual_spread_pct is None:
                    actual_spread_str = "-"
                else:
                    spread_val_str = f"{actual_spread_pct:+.3f}%"
                    # 平仓用绿色突出真实价差；开仓保持青色
                    if is_open:
                        actual_spread_str = f"[cyan]{spread_val_str}[/cyan]"
                    else:
                        actual_spread_str = f"[green]{spread_val_str}[/green]"
                
                table.add_row(
                    time_str,
                    symbol,
                    type_str,
                    quantity_str,
                    price_buy_str,
                    price_sell_str,
                    actual_spread_str,
                    exchange_buy,
                    exchange_sell,
                    status_str,
                    error_str
                )
        
        return Panel(table, title="[bold white]📋 执行记录（最近20条）[/bold white]", border_style="yellow")

    @staticmethod
    def create_multi_leg_table(rows: List[Dict], total_pairs: int = 0) -> Panel:
        """创建多腿套利组合实时价差表"""
        from rich.box import SQUARE
        table = Table(
            show_header=True,
            header_style="bold white",
            border_style="dim white",
            box=SQUARE,
            show_lines=True,
            padding=(0, 1),
            collapse_padding=True
        )

        table.add_column("组合", style="white", width=20, no_wrap=True, overflow="ellipsis")
        table.add_column("买入腿", style="white", width=20, no_wrap=True, overflow="ellipsis")
        table.add_column("买价×数量", style="white", width=40, no_wrap=True, overflow="ellipsis")
        table.add_column("卖出腿", style="white", width=20, no_wrap=True, overflow="ellipsis")
        table.add_column("卖价×数量", style="white", width=40, no_wrap=True, overflow="ellipsis")
        table.add_column("实时价差%", style="white", justify="right", width=10, no_wrap=True)
        table.add_column("平仓价差%", style="white", justify="right", width=10, no_wrap=True)
        table.add_column("目标价差%", style="white", justify="right", width=10, no_wrap=True)

        entries = rows if rows else []
        if not entries and total_pairs == 0:
            table.add_row("未配置多腿套利组合", "—", "—", "—", "—", "—", "—", "—")
        else:
            if not entries and total_pairs > 0:
                table.add_row("等待实时数据...", "—", "—", "—", "—", "—", "—", "—")
            else:
                for row in entries:
                    combo_name = row.get("description") or row.get("pair_id")
                    
                    # 🔥 使用买入腿和卖出腿的信息（已经根据最优方向确定）
                    buying_leg_exchange = row.get("buying_leg_exchange", "")
                    buying_leg_symbol = row.get("buying_leg_symbol", "")
                    selling_leg_exchange = row.get("selling_leg_exchange", "")
                    selling_leg_symbol = row.get("selling_leg_symbol", "")
                    
                    buy_leg = f"{buying_leg_exchange}/{buying_leg_symbol}"
                    sell_leg = f"{selling_leg_exchange}/{selling_leg_symbol}"
                    
                    # 买入腿显示：Ask(买入价) / Bid(卖出价)
                    buying_leg_ask = row.get("buying_leg_ask")
                    buying_leg_bid = row.get("buying_leg_bid")
                    buying_leg_ask_size = row.get("buying_leg_ask_size")
                    buying_leg_bid_size = row.get("buying_leg_bid_size")
                    
                    if buying_leg_ask is not None and buying_leg_bid is not None:
                        buy_quote = f"${buying_leg_ask:,.2f}×{buying_leg_ask_size:.4f} / ${buying_leg_bid:,.2f}×{buying_leg_bid_size:.4f}"
                    else:
                        buy_quote = "—"
                    
                    # 卖出腿显示：Ask(买入价) / Bid(卖出价)
                    selling_leg_ask = row.get("selling_leg_ask")
                    selling_leg_bid = row.get("selling_leg_bid")
                    selling_leg_ask_size = row.get("selling_leg_ask_size")
                    selling_leg_bid_size = row.get("selling_leg_bid_size")
                    
                    if selling_leg_ask is not None and selling_leg_bid is not None:
                        sell_quote = f"${selling_leg_ask:,.2f}×{selling_leg_ask_size:.4f} / ${selling_leg_bid:,.2f}×{selling_leg_bid_size:.4f}"
                    else:
                        sell_quote = "—"

                    # 开仓价差（实时价差）
                    spread_pct = row.get("spread_pct")
                    if spread_pct is None:
                        spread_text = Text("—", style="dim white")
                    else:
                        if spread_pct >= 0.5:
                            spread_style = "bold white"
                        elif spread_pct >= 0.2:
                            spread_style = "white"
                        else:
                            spread_style = "dim white"
                        spread_text = Text(f"{spread_pct:+.3f}%", style=spread_style)

                    # 平仓价差（反向价差）
                    closing_spread_pct = row.get("closing_spread_pct")
                    if closing_spread_pct is None:
                        closing_text = Text("—", style="dim white")
                    else:
                        # 平仓价差通常是负数（亏损），越接近0越好
                        if closing_spread_pct >= 0:
                            # 正数表示平仓也能盈利（价差反转）
                            closing_style = "bold green"
                        elif closing_spread_pct >= -0.01:
                            # 接近0，亏损很小
                            closing_style = "green"
                        elif closing_spread_pct >= -0.05:
                            # 小幅亏损
                            closing_style = "white"
                        else:
                            # 较大亏损
                            closing_style = "dim white"
                        closing_text = Text(f"{closing_spread_pct:+.3f}%", style=closing_style)

                    min_spread = row.get("min_spread_pct")
                    min_spread_str = f"{min_spread:.2f}%" if isinstance(min_spread, (int, float)) and min_spread is not None else "—"

                    table.add_row(
                        combo_name,
                        buy_leg,
                        buy_quote,
                        sell_leg,
                        sell_quote,
                        spread_text,
                        closing_text,
                        min_spread_str
                    )

        return Panel(table, title="[bold white]多腿套利实时价差[/bold white]", border_style="white")

