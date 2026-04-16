#!/bin/bash
# 快速部署：渲染（含LLM升级）+ 部署 + Telegram
set -e
cd "$(dirname "$0")"

# 启动代理
PROXY_PID=""
if ! curl -s --connect-timeout 3 "http://localhost:3456/v1/models" > /dev/null 2>&1; then
    [ -f claude_proxy.py ] && { python3 -u claude_proxy.py & PROXY_PID=$!; for i in $(seq 1 30); do curl -s --connect-timeout 2 "http://localhost:3456/health" > /dev/null 2>&1 && break; sleep 1; done; }
fi
trap '[ -n "$PROXY_PID" ] && kill "$PROXY_PID" 2>/dev/null' EXIT

# 渲染
python3 -u briefing_renderer.py --hours 48

# 部署
[ -f "$HOME/.config/ai-briefing/.env" ] && source "$HOME/.config/ai-briefing/.env"
REPO_URL=$(cd output && git remote get-url origin 2>/dev/null || echo "${DEPLOY_REPO_URL:-}")
if [ -n "$REPO_URL" ] && [ -f output/index.html ]; then
    TMP="/tmp/ai_deploy_$$"; rm -rf "$TMP"
    git clone --depth 1 "$REPO_URL" "$TMP"
    cp output/index.html output/modal_data.js output/stats.json "$TMP/" 2>/dev/null
    [ -d output/archive ] && mkdir -p "$TMP/archive" && cp -r output/archive/* "$TMP/archive/" 2>/dev/null
    cd "$TMP" && git config user.email "hawaha113@protonmail.com" && git config user.name "hawaha112"
    git add -A && git diff --cached --quiet || { git commit -m "Fix: $(date '+%Y-%m-%d %H:%M')"; git push origin main; }
    cd - > /dev/null; rm -rf "$TMP"
    echo "✅ 部署成功"
fi

# Telegram
if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
    N=$(python3 -c "import json;print(json.load(open('output/stats.json'))['article_count'])" 2>/dev/null||echo "?")
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" -H "Content-Type: application/json" \
        -d "{\"chat_id\":\"${TG_CHAT_ID}\",\"text\":\"<b>AI 早报 · $(date '+%Y年%m月%d日')（修复版）</b>\n\n收录 ${N} 条 AI 资讯\n${BRIEFING_URL:+<a href=\\\"${BRIEFING_URL}\\\">点击阅读</a>}\",\"parse_mode\":\"HTML\"}" > /dev/null 2>&1
    echo "✅ Telegram 已发送"
fi
echo "✅ 完成"
