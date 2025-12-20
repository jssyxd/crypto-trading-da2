#!/usr/bin/env python3
"""
套利监控系统 V2 - 纯滚动模式启动脚本

完全放弃 Rich UI，使用纯粹的 print() 输出
- 零延迟
- 最实时
- 最简单
"""

# 🔥 加载环境变量（必须在其他导入之前）
from dotenv import load_dotenv
from pathlib import Path
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)

import asyncio
import sys
import signal

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core.services.arbitrage_monitor_v2.core.orchestrator_simple import ArbitrageOrchestratorSimple
from core.services.arbitrage_monitor_v2.config.debug_config import DebugConfig


async def main():
    """主函数"""
    # 解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(description='套利监控系统 V2 - 纯滚动模式')
    parser.add_argument('--debug', action='store_true', help='启用 Debug 模式')
    parser.add_argument('--config', type=str, help='配置文件路径')
    parser.add_argument('--symbols', type=str, nargs='+', help='监控的交易对（如 BTC-USDC-PERP）')
    args = parser.parse_args()
    
    # 创建 Debug 配置
    if args.debug:
        debug_config = DebugConfig.create_detail()
        print("🐛 Debug 模式已启用")
    else:
        debug_config = DebugConfig()
    
    # 配置文件路径
    config_path = Path(args.config) if args.config else None
    
    # 创建调度器（纯打印模式）
    orchestrator = ArbitrageOrchestratorSimple(
        config_path=config_path,
        debug_config=debug_config
    )
    
    # 如果指定了交易对，覆盖配置
    if args.symbols:
        orchestrator.config.symbols = args.symbols
        print(f"📊 监控交易对: {', '.join(args.symbols)}")
    
    # 启动系统
    try:
        await orchestrator.start()
        
        # 保持运行（Ctrl+C 会触发 KeyboardInterrupt）
        while orchestrator.running:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        print("\n\n🛑 用户中断，正在关闭...")
    except Exception as e:
        print(f"\n\n❌ 系统错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 确保停止系统
        if orchestrator.running:
            await orchestrator.stop()


if __name__ == "__main__":
    print("\n" + "=" * 140)
    print(f"{'🚀 套利监控系统 V2 - 纯滚动模式（完全放弃 Rich UI）':^140}")
    print("=" * 140)
    print()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n✅ 程序已退出")

