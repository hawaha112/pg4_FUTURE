#!/bin/bash
# ============================================================
# AI Morning Briefing - 完整测试：采集 + LLM分析 + 渲染 + 部署
# ============================================================
set -e

cd "$(dirname "$0")"
echo "========================================"
echo "🚀 完整出报测试 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# ── 第一步：检查/启动 LLM 代理 ──
LLM_URL=$(python3 -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://localhost:3456/v1")
echo "检查 LLM 服务 ($LLM_URL)..."

PROXY_PID=""
PROXY_STARTED_BY_US=false

if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    echo "  ✅ LLM 服务已在运行"
else
    if [ -f "claude_proxy.py" ]; then
        echo "  🔄 启动 claude_proxy.py..."
        python3 -u claude_proxy.py &
        PROXY_PID=$!
        PROXY_STARTED_BY_US=true

        for i in $(seq 1 30); do
            if curl -s --connect-timeout 2 "http://localhost:3456/health" > /dev/null 2>&1; then
                echo "  ✅ 代理就绪（${i}s）"
                break
            fi
            sleep 1
        done

        if ! curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
            echo "  ❌ 代理启动超时"
            echo "  尝试无 LLM 模式继续..."
            NO_LLM="--no-llm"
            if [ -n "$PROXY_PID" ]; then
                kill "$PROXY_PID" 2>/dev/null || true
                PROXY_PID=""
            fi
        fi
    else
        echo "  ⚠️ 无 claude_proxy.py，使用 --no-llm"
        NO_LLM="--no-llm"
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

# ── 第二步：采集 ──
echo ""
echo "========================================"
echo "📡 第二步：采集最新数据..."
echo "========================================"
python3 -u collector.py ${NO_LLM:-} 2>&1 || {
    echo "  ⚠️ 采集失败，使用事件库中现有数据继续"
}

# ── 第三步：渲染 ──
echo ""
echo "========================================"
echo "🎨 第三步：渲染页面..."
echo "========================================"
python3 -u briefing_renderer.py --hours 48 2>&1

# ── 第四步：部署 ──
echo ""
echo "========================================"
echo "🚀 第四步：部署到 GitHub Pages..."
echo "========================================"

# 加载环境变量
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

DEPLOY_OK=false
DEPLOY_TMP="/tmp/ai_briefing_deploy_$$"
REPO_URL=$(cd output && git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REPO_URL" ]; then
    REPO_URL="${DEPLOY_REPO_URL:-}"
fi

# 自动修复 output/.git 损坏
if [ -d "output/.git/rebase-merge" ] || [ -f "output/.git/index.lock" ]; then
    echo "  ⚠️ 修复 output/.git 状态..."
    rm -f "output/.git/index.lock" 2>/dev/null || true
    rm -rf "output/.git/rebase-merge" 2>/dev/null || true
    cd output && git rebase --abort 2>/dev/null || true
    cd ..
fi

if [ -n "$REPO_URL" ] && [ -f "output/index.html" ]; then
    rm -rf "$DEPLOY_TMP"
    if git clone --depth 1 "$REPO_URL" "$DEPLOY_TMP" 2>&1; then
        cp output/index.html "$DEPLOY_TMP/"
        cp output/modal_data.js "$DEPLOY_TMP/" 2>/dev/null || true
        cp output/stats.json "$DEPLOY_TMP/" 2>/dev/null || true
        if [ -d "output/archive" ]; then
            mkdir -p "$DEPLOY_TMP/archive"
            cp -r output/archive/* "$DEPLOY_TMP/archive/" 2>/dev/null || true
        fi
        cd "$DEPLOY_TMP"
        git config user.email "hawaha113@protonmail.com"
        git config user.name "hawaha112"
        git add -A
        if ! git diff --cached --quiet; then
            git commit -m "Daily update: $(date '+%Y-%m-%d %H:%M')" 2>&1
            if git push origin main 2>&1; then
                echo "  ✅ 部署成功"
                DEPLOY_OK=true
            else
                echo "  ❌ push 失败"
            fi
        else
            echo "  ✅ 无新变更"
            DEPLOY_OK=true
        fi
        cd "$(dirname "$0")"
        rm -rf "$DEPLOY_TMP"
    else
        echo "  ❌ 克隆远程仓库失败"
    fi
else
    echo "  ⏭️ 跳过部署（无 git 配置: REPO_URL='$REPO_URL'）"
fi

# ── 第五步：归档 ──
ARCHIVE_DIR="output/archive"
TODAY_DATE=$(date '+%Y-%m-%d')
mkdir -p "$ARCHIVE_DIR"
if [ -f "output/index.html" ]; then
    cp "output/index.html" "$ARCHIVE_DIR/${TODAY_DATE}.html"
    cp "output/modal_data.js" "$ARCHIVE_DIR/${TODAY_DATE}_modal.js" 2>/dev/null || true
    echo "  📁 已归档: archive/${TODAY_DATE}.html"
fi

# ── 第六步：Telegram 通知 ──
send_tg() {
    if [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
        echo "  ⏭️ 跳过 Telegram（未配置）"
        return 0
    fi
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"text\": $(printf '%s' "$message" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": false}" \
        > /dev/null 2>&1 && echo "  ✅ Telegram 已发送" || echo "  ⚠️ Telegram 发送失败"
}

ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('output/stats.json'))['article_count'])" 2>/dev/null || echo "?")
TODAY=$(date '+%Y年%m月%d日')
BRIEFING_URL="${BRIEFING_URL:-}"

if [ "$DEPLOY_OK" = true ] && [ -n "$BRIEFING_URL" ]; then
    send_tg "<b>AI 早报 · ${TODAY}</b>

今日共收录 ${ARTICLE_COUNT} 条 AI 资讯

<a href=\"${BRIEFING_URL}\">点击阅读今日早报</a>"
else
    send_tg "<b>AI 早报 · ${TODAY}</b>

已渲染 ${ARTICLE_COUNT} 条资讯
${DEPLOY_OK:+部署成功}${DEPLOY_OK:-部署未执行或失败}"
fi

echo ""
echo "========================================"
echo "✅ 完整测试结束！共 ${ARTICLE_COUNT} 条 AI 资讯"
echo "========================================"
