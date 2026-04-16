#!/bin/bash
# 测试脚本 - 完整 LLM 流水线测试
# 用法: bash run_test.sh

set -e
cd "$(dirname "$0")"

echo "🧪 AI 早报完整测试"
echo "================================"

# 1. 清理旧数据（保留 dedup 历史）
echo "🗑️ 清理旧数据..."
rm -f events.db events.db-wal events.db-shm
rm -f llm_cache.db llm_cache.db-wal llm_cache.db-shm
rm -f dedup.db dedup.db-wal dedup.db-shm

# 2. 杀掉旧代理，启动新代理
echo "🔄 重启 LLM 代理..."
pkill -f claude_proxy.py 2>/dev/null || true
sleep 2
python3 -u claude_proxy.py &
PROXY_PID=$!
echo "  代理 PID: $PROXY_PID"

# 等待代理就绪
for i in $(seq 1 15); do
    if curl -s --connect-timeout 2 "http://localhost:3456/health" > /dev/null 2>&1; then
        echo "  ✅ 代理就绪（${i}s）"
        break
    fi
    sleep 1
done

if ! curl -s --connect-timeout 5 "http://localhost:3456/health" > /dev/null 2>&1; then
    echo "  ❌ 代理启动失败"
    exit 1
fi

# 3. 运行采集 + LLM 分析
echo ""
echo "📡 开始采集 + LLM 分析（预计 15-30 分钟）..."
echo "   请耐心等待，每篇文章 LLM 分析约需 30-120 秒"
echo ""
python3 -u collector.py 2>&1 | tee /tmp/ai_test_collector.log

# 4. 渲染页面
echo ""
echo "🎨 渲染页面..."
python3 -u briefing_renderer.py 2>&1

# 5. 显示统计
echo ""
echo "📊 结果统计:"
python3 -c "
import json
s = json.load(open('output/stats.json'))
print(f'  文章数: {s[\"article_count\"]}')
print(f'  总事件: {s[\"event_db_stats\"][\"total_events\"]}')
print(f'  已分析: {s[\"event_db_stats\"][\"analyzed\"]}')
print(f'  文件大小: {s[\"file_size_kb\"]:.1f} KB')
"

# 6. 部署到 GitHub Pages
echo ""
echo "🚀 部署到 GitHub Pages..."
DEPLOY_TMP="/tmp/ai_briefing_test_deploy_$$"
REPO_URL=$(cd output && git remote get-url origin 2>/dev/null || echo "")
if [ -n "$REPO_URL" ] && [ -f "output/index.html" ]; then
    rm -rf "$DEPLOY_TMP"
    git clone --depth 1 "$REPO_URL" "$DEPLOY_TMP"
    cp output/index.html "$DEPLOY_TMP/"
    cp output/modal_data.js "$DEPLOY_TMP/" 2>/dev/null || true
    cp output/stats.json "$DEPLOY_TMP/" 2>/dev/null || true
    cd "$DEPLOY_TMP"
    git config user.email "hawaha113@protonmail.com"
    git config user.name "hawaha112"
    git add -A
    if ! git diff --cached --quiet; then
        git commit -m "Test run: $(date '+%Y-%m-%d %H:%M')"
        git push origin main
        echo "  ✅ 部署成功"
    else
        echo "  无新变更"
    fi
    cd -
    rm -rf "$DEPLOY_TMP"
else
    echo "  ⚠️ 无法部署（缺少 git 配置）"
fi

# 7. 发送 TG 通知
echo ""
echo "📱 发送 Telegram 通知..."
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi
# 兜底：从 run_once.sh 读取
if [ -z "$TG_BOT_TOKEN" ]; then
    TG_BOT_TOKEN="8768397666:AAFEuiL5KnXprtkxjJtZFnn5P0e3Bk4qA-M"
    TG_CHAT_ID="8140776479"
    BRIEFING_URL="https://hawaha112.github.io/ai-morning-briefing/"
fi

TODAY=$(date '+%Y年%m月%d日')
ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('output/stats.json'))['article_count'])" 2>/dev/null || echo "?")

curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"text\": \"<b>🧪 AI 早报测试 · ${TODAY}</b>\n\n共收录 ${ARTICLE_COUNT} 条 AI 资讯（LLM 深度分析版）\n\n<a href=\\\"${BRIEFING_URL}\\\">点击查看测试结果</a>\", \"parse_mode\": \"HTML\", \"disable_web_page_preview\": false}" || true

# 8. 关闭代理
echo ""
echo "🛑 关闭代理..."
kill $PROXY_PID 2>/dev/null || true

echo ""
echo "✅ 测试完成！请在 Telegram 查看结果。"
