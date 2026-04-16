#!/bin/bash
# ============================================================
# 仅渲染 + 部署（使用事件库现有数据，含 Level 0 升级）
# ============================================================
set -e
cd "$(dirname "$0")"

echo "========================================"
echo "🎨 渲染 + 部署 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# 启动 LLM 代理（用于 Level 0 升级和速览生成）
PROXY_PID=""
PROXY_STARTED_BY_US=false
LLM_URL=$(python3 -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://localhost:3456/v1")

if ! curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    if [ -f "claude_proxy.py" ]; then
        echo "🔄 启动 claude_proxy.py..."
        python3 -u claude_proxy.py &
        PROXY_PID=$!
        PROXY_STARTED_BY_US=true
        for i in $(seq 1 30); do
            if curl -s --connect-timeout 2 "http://localhost:3456/health" > /dev/null 2>&1; then
                echo "✅ 代理就绪（${i}s）"
                break
            fi
            sleep 1
        done
    fi
fi

cleanup_proxy() {
    if [ "$PROXY_STARTED_BY_US" = true ] && [ -n "$PROXY_PID" ]; then
        echo "关闭代理 (PID: $PROXY_PID)..."
        kill "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup_proxy EXIT

# 渲染（含 Level 0 升级）
echo ""
echo "🎨 渲染页面（含 Level 0 升级）..."
python3 -u briefing_renderer.py --hours 48 2>&1

# 部署
ENV_FILE="$HOME/.config/ai-briefing/.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

echo ""
echo "🚀 部署到 GitHub Pages..."
DEPLOY_TMP="/tmp/ai_briefing_deploy_$$"
REPO_URL=$(cd output && git remote get-url origin 2>/dev/null || echo "")
[ -z "$REPO_URL" ] && REPO_URL="${DEPLOY_REPO_URL:-}"

if [ -n "$REPO_URL" ] && [ -f "output/index.html" ]; then
    rm -rf "$DEPLOY_TMP"
    if git clone --depth 1 "$REPO_URL" "$DEPLOY_TMP" 2>&1; then
        cp output/index.html "$DEPLOY_TMP/"
        cp output/modal_data.js "$DEPLOY_TMP/" 2>/dev/null || true
        cp output/stats.json "$DEPLOY_TMP/" 2>/dev/null || true
        [ -d "output/archive" ] && mkdir -p "$DEPLOY_TMP/archive" && cp -r output/archive/* "$DEPLOY_TMP/archive/" 2>/dev/null || true
        cd "$DEPLOY_TMP"
        git config user.email "hawaha113@protonmail.com"
        git config user.name "hawaha112"
        git add -A
        if ! git diff --cached --quiet; then
            git commit -m "Update: $(date '+%Y-%m-%d %H:%M')" 2>&1
            git push origin main 2>&1 && echo "✅ 部署成功" || echo "❌ push 失败"
        else
            echo "✅ 无新变更"
        fi
        cd "$(dirname "$0")"
        rm -rf "$DEPLOY_TMP"
    fi
fi

# Telegram 通知
send_tg() {
    [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ] && return 0
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"text\": $(printf '%s' "$1" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": false}" \
        > /dev/null 2>&1 && echo "✅ Telegram 已发送" || true
}

ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('output/stats.json'))['article_count'])" 2>/dev/null || echo "?")
TODAY=$(date '+%Y年%m月%d日')
BRIEFING_URL="${BRIEFING_URL:-}"
send_tg "<b>AI 早报 · ${TODAY}（修复版）</b>

收录 ${ARTICLE_COUNT} 条 AI 资讯，已修复中文标题、发布时间和深度内容
${BRIEFING_URL:+<a href=\"${BRIEFING_URL}\">点击阅读</a>}"

# 归档
mkdir -p output/archive
TODAY_DATE=$(date '+%Y-%m-%d')
cp output/index.html "output/archive/${TODAY_DATE}.html" 2>/dev/null || true
cp output/modal_data.js "output/archive/${TODAY_DATE}_modal.js" 2>/dev/null || true

echo ""
echo "========================================"
echo "✅ 完成！共 ${ARTICLE_COUNT} 条"
echo "========================================"
