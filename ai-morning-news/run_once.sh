#!/bin/bash
# 一键测试脚本：创建 .env 配置 + 运行每日流水线

# 1. 创建 Telegram 配置（从环境变量读取，不再硬编码 token —— 公开仓安全）
#    用前先: export TG_BOT_TOKEN=... ; export TG_CHAT_ID=...
mkdir -p ~/.config/ai-briefing
: "${TG_BOT_TOKEN:?请先 export TG_BOT_TOKEN（BotFather 的 bot token）}"
: "${TG_CHAT_ID:?请先 export TG_CHAT_ID}"
cat > ~/.config/ai-briefing/.env << ENVEOF
TG_BOT_TOKEN="${TG_BOT_TOKEN}"
TG_CHAT_ID="${TG_CHAT_ID}"
BRIEFING_URL="${BRIEFING_URL:-https://hawaha112.github.io/ai-morning-briefing/}"
ENVEOF
echo "✅ .env 配置已创建（从环境变量）"

# 2. 运行每日流水线
cd "$(dirname "$0")"
echo "🚀 开始执行每日流水线..."
bash run_daily.sh
echo "✅ 执行完毕"
