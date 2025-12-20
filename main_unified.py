#!/usr/bin/env python3
"""
分段套利系统统一启动脚本（重构版）

使用方式：
    python main_unified.py

功能：
    - 使用新的统一决策引擎（总量驱动算法）
    - 支持多交易对独立配置
    - 支持剥头皮模式
    - 支持拆单执行
"""

import asyncio
import argparse
import sys
import os
from pathlib import Path
from typing import Optional

# 🔥 SSL 证书自动修复（放在所有网络库导入之前）
# 强制使用 certifi 提供的根证书，解决 macOS 外部终端 SSL 验证失败的问题
try:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    # print(f"🔐 SSL 证书路径已强制指定: {certifi.where()}")
except ImportError:
    print("⚠️  未找到 certifi 库，SSL 证书验证可能会在外部终端失败")

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

# 🔥 加载环境变量（必须在导入其他模块之前）
from dotenv import load_dotenv
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ 已加载环境变量: {env_path}")
else:
    print(f"⚠️  未找到 .env 文件: {env_path}")
    print("💡 如需配置API密钥，请创建 .env 文件")

from core.services.arbitrage_monitor_v2.core.unified_orchestrator import UnifiedOrchestrator
from core.services.arbitrage_monitor_v2.config.debug_config import DebugConfig


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="分段套利系统统一启动脚本（重构版）"
    )
    
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/arbitrage/arbitrage_segmented.yaml"),
        help="分段配置文件路径"
    )
    
    parser.add_argument(
        "--monitor-config",
        type=Path,
        default=Path("config/arbitrage/monitor_v2.yaml"),
        help="监控配置文件路径"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试模式"
    )
    
    return parser.parse_args()


async def main():
    """主入口函数"""
    args = parse_args()
    
    # 配置Debug选项
    if args.debug:
        debug_config = DebugConfig.create_basic()
        print("🔧 调试模式已启用 (Basic)")
    else:
        debug_config = DebugConfig.create_production()
    orchestrator: Optional[UnifiedOrchestrator] = None
    try:
        # 初始化统一调度器
        orchestrator = UnifiedOrchestrator(
            segmented_config_path=args.config,
            monitor_config_path=args.monitor_config,
            debug_config=debug_config
        )

        # 启动系统
        await orchestrator.start()
        
        # 保持运行
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        print("\n🛑 用户停止程序")
    except Exception as e:
        print(f"\n❌ 程序异常退出: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 优雅关闭逻辑（如果 orchestrator 提供了 stop 方法）
        if orchestrator and hasattr(orchestrator, 'stop'):
            print("正在关闭调度器...")
            await orchestrator.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
