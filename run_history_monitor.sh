#!/bin/bash

# 历史数据监控工具启动脚本
# 用法: ./run_history_monitor.sh

cd "$(dirname "$0")"

echo "🚀 启动历史数据监控工具..."
echo "📋 提示: 按 Ctrl+C 停止监控"
echo ""

python3 tools/history_data_monitor.py

