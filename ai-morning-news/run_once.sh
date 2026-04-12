#!/bin/bash
# 一键测试脚本：创建 .env 配置 + 运行每日流水线

# 1. 创建 Telegram 配置
mkdir -p ~/.config/ai-briefing
cat > ~/.config/ai-briefing/.env << 'ENVEOF'
TG_BOT_TOKEN="8768397666:AAFEuiL5KnXprtkxjJtZFnn5P0e3Bk4qA-M"
TG_CHAT_ID="8140776479"
BRIEFING_URL="https://hawaha112.github.io/ai-morning-briefing/"
ENVEOF
echo "✅ .env 配置已创建"

# 2. 运行每日流水线
cd "$(dirname "$0")"
echo "🚀 开始执行每日流水线..."
bash run_daily.sh
echo "✅ 执行完毕"
