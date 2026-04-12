#!/bin/bash
# ============================================================
# AI Morning Briefing - 每日定时任务运行脚本
# 由 launchd 在每天早上 7:00 自动调用
# ============================================================

set -e

# ────────────────────────────────────────────────
# 互斥锁（PID 文件方案，macOS 无 flock）
# 防止 launchd catch-up / 手动重复触发时多个实例
# 并发运行互相污染 dedup.db 覆盖好结果。
# ────────────────────────────────────────────────
_SCRIPT_DIR_EARLY="$(cd "$(dirname "$0")" && pwd)"
_LOCK_LOG="$_SCRIPT_DIR_EARLY/daily_run.log"
PIDFILE="$_SCRIPT_DIR_EARLY/.run_daily.pid"
if [ -f "$PIDFILE" ]; then
    _OLD_PID="$(cat "$PIDFILE" 2>/dev/null || echo)"
    if [ -n "$_OLD_PID" ] && kill -0 "$_OLD_PID" 2>/dev/null; then
        {
            echo ""
            echo "⚠️ $(date '+%Y-%m-%d %H:%M:%S') 另一个 run_daily.sh 实例 (PID $_OLD_PID) 仍在运行，本次跳过"
        } >> "$_LOCK_LOG"
        exit 0
    fi
    # 旧 PID 文件是孤儿（上次进程被 kill 掉）
    rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

# 项目路径：自动定位到脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
LOG_FILE="$PROJECT_DIR/daily_run.log"

# 从外部 .env 文件加载敏感配置（TG_BOT_TOKEN, TG_CHAT_ID, BRIEFING_URL）
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    # shellcheck source=/dev/null
    source "$ENV_FILE"
fi

# Telegram 发送函数（token 未配置时静默跳过）
send_tg() {
    if [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
        return 0
    fi
    local message="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"text\": $(printf '%s' "$message" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": false}" \
        > /dev/null 2>&1 || true
}

# 记录时间戳
{
    echo ""
    echo "========================================"
    echo "运行时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================"
} >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 第一步：自动启动 Claude Max API 代理
PROXY_DIR="$PROJECT_DIR/claude-max-api-proxy"
PROXY_PID=""
PROXY_PORT=3456
PROXY_STARTED_BY_US=false

LLM_URL=$(python3 -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://localhost:3456/v1")
echo "检查 LLM 服务 ($LLM_URL)..." >> "$LOG_FILE"

# 先检查 LLM 服务是否已经在运行（可能手动启动了代理或用的其他服务）
if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    echo "  LLM 服务已在运行" >> "$LOG_FILE"
else
    # 尝试自动启动 claude_proxy.py
    if [ -f "$PROJECT_DIR/claude_proxy.py" ]; then
        echo "  启动 claude_proxy.py..." >> "$LOG_FILE"
        python3 -u "$PROJECT_DIR/claude_proxy.py" >> "$LOG_FILE" 2>&1 &
        PROXY_PID=$!
        PROXY_STARTED_BY_US=true

        # 等待代理就绪（最多 30 秒）
        for i in $(seq 1 30); do
            if curl -s --connect-timeout 2 "http://localhost:${PROXY_PORT}/health" > /dev/null 2>&1; then
                echo "  代理已就绪（等待 ${i}s）" >> "$LOG_FILE"
                break
            fi
            sleep 1
        done

        # 再次检查
        if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
            echo "  LLM 服务正常（via claude-max-api-proxy）" >> "$LOG_FILE"
        else
            echo "  代理启动超时，使用 --no-llm 模式" >> "$LOG_FILE"
            NO_LLM="--no-llm"
            # 清理失败的进程
            if [ -n "$PROXY_PID" ]; then
                kill "$PROXY_PID" 2>/dev/null || true
                PROXY_PID=""
            fi
        fi
    else
        echo "  未找到 claude_proxy.py" >> "$LOG_FILE"
        echo "  使用 --no-llm 模式" >> "$LOG_FILE"
        NO_LLM="--no-llm"
    fi
fi

# 注册退出钩子：脚本结束时自动关闭我们启动的代理
cleanup_proxy() {
    if [ "$PROXY_STARTED_BY_US" = true ] && [ -n "$PROXY_PID" ]; then
        echo "关闭 claude-max-api-proxy (PID: $PROXY_PID)..." >> "$LOG_FILE"
        kill "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup_proxy EXIT

# 第二步：运行新闻抓取脚本（-u 禁用输出缓冲，确保日志实时写入）
# 支持 checkpoint 断点续跑：首次运行失败后自动 --resume 重试一次
echo "开始抓取新闻..." >> "$LOG_FILE"
if ! python3 -u "$PROJECT_DIR/fetch_news.py" ${NO_LLM:-} >> "$LOG_FILE" 2>&1; then
    FETCH_STATUS=$?
    echo "  ⚠️ 首次运行失败（退出码: $FETCH_STATUS），尝试从 checkpoint 恢复..." >> "$LOG_FILE"
    sleep 5
    if ! python3 -u "$PROJECT_DIR/fetch_news.py" --resume ${NO_LLM:-} >> "$LOG_FILE" 2>&1; then
        RETRY_STATUS=$?
        echo "  ❌ 重试仍然失败（退出码: $RETRY_STATUS）" >> "$LOG_FILE"
        send_tg "<b>AI 早报生成失败</b>
首次退出码: $FETCH_STATUS，重试退出码: $RETRY_STATUS
请检查 daily_run.log"
        exit 1
    fi
    echo "  ✅ 从 checkpoint 恢复成功" >> "$LOG_FILE"
fi
echo "  新闻抓取完成" >> "$LOG_FILE"

# 归档当天日报到 archive/ 目录（保留历史，方便回顾和生成周报）
ARCHIVE_DIR="$PROJECT_DIR/output/archive"
TODAY_DATE=$(date '+%Y-%m-%d')
mkdir -p "$ARCHIVE_DIR"
if [ -f "$PROJECT_DIR/output/index.html" ]; then
    cp "$PROJECT_DIR/output/index.html" "$ARCHIVE_DIR/${TODAY_DATE}.html"
    cp "$PROJECT_DIR/output/modal_data.js" "$ARCHIVE_DIR/${TODAY_DATE}_modal.js" 2>/dev/null || true
    echo "  已归档日报: archive/${TODAY_DATE}.html" >> "$LOG_FILE"
fi

# 从 stats.json 读取文章数量
STATS_FILE="$PROJECT_DIR/output/stats.json"
if [ -f "$STATS_FILE" ]; then
    ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('$STATS_FILE'))['article_count'])" 2>/dev/null || echo "?")
else
    ARTICLE_COUNT="?"
fi

# 健康检查：文章数过低时告警
HEALTH_WARNING=""
if [ "$ARTICLE_COUNT" != "?" ] && [ "$ARTICLE_COUNT" -lt 3 ] 2>/dev/null; then
    echo "  文章数异常偏低: $ARTICLE_COUNT" >> "$LOG_FILE"
    HEALTH_WARNING="文章数异常偏低（仅 ${ARTICLE_COUNT} 条），部分 RSS 源可能不可用"
fi

# 第三步：部署到 GitHub Pages（使用临时目录避免冲突）
DEPLOY_OK=false
echo "开始部署..." >> "$LOG_FILE"

DEPLOY_TMP="/tmp/ai_briefing_deploy_$$"
# 尝试从 output/.git 获取远程 URL，如果失败则用硬编码备选
REPO_URL=$(cd "$PROJECT_DIR/output" && git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REPO_URL" ]; then
    REPO_URL="${DEPLOY_REPO_URL:-}"
fi
# 自动修复 output/.git 的损坏状态（rebase 卡住等）
if [ -d "$PROJECT_DIR/output/.git/rebase-merge" ] || [ -f "$PROJECT_DIR/output/.git/index.lock" ]; then
    echo "  ⚠️ 检测到 output/.git 状态异常，尝试修复..." >> "$LOG_FILE"
    rm -f "$PROJECT_DIR/output/.git/index.lock" 2>/dev/null || true
    rm -rf "$PROJECT_DIR/output/.git/rebase-merge" 2>/dev/null || true
    cd "$PROJECT_DIR/output" && git rebase --abort 2>/dev/null || true
    cd "$PROJECT_DIR"
fi

if [ -n "$REPO_URL" ] && [ -f "$PROJECT_DIR/output/index.html" ]; then
    # 克隆最新远程到临时目录，避免本地状态冲突
    rm -rf "$DEPLOY_TMP"
    if git clone --depth 1 "$REPO_URL" "$DEPLOY_TMP" >> "$LOG_FILE" 2>&1; then
        # 复制生成的文件
        cp "$PROJECT_DIR/output/index.html" "$DEPLOY_TMP/"
        cp "$PROJECT_DIR/output/modal_data.js" "$DEPLOY_TMP/" 2>/dev/null || true
        cp "$PROJECT_DIR/output/stats.json" "$DEPLOY_TMP/" 2>/dev/null || true
        # 复制归档文件
        if [ -d "$PROJECT_DIR/output/archive" ]; then
            mkdir -p "$DEPLOY_TMP/archive"
            cp -r "$PROJECT_DIR/output/archive/"* "$DEPLOY_TMP/archive/" 2>/dev/null || true
        fi
        cd "$DEPLOY_TMP"
        git config user.email "hawaha113@protonmail.com"
        git config user.name "hawaha112"
        git add -A
        if ! git diff --cached --quiet; then
            git commit -m "Daily update: $(date '+%Y-%m-%d %H:%M')" >> "$LOG_FILE" 2>&1
            if git push origin main >> "$LOG_FILE" 2>&1; then
                echo "  部署成功" >> "$LOG_FILE"
                DEPLOY_OK=true
            else
                echo "  push 失败" >> "$LOG_FILE"
            fi
        else
            echo "  没有新的变更需要部署" >> "$LOG_FILE"
            DEPLOY_OK=true
        fi
        cd "$PROJECT_DIR"
        rm -rf "$DEPLOY_TMP"
    else
        echo "  克隆远程仓库失败" >> "$LOG_FILE"
    fi
else
    echo "  output 目录没有 git 配置或 index.html 不存在，跳过部署" >> "$LOG_FILE"
fi

# 第四步：Telegram 通知
TODAY=$(date '+%Y年%m月%d日')
BRIEFING_URL="${BRIEFING_URL:-}"
ARCHIVE_URL="${BRIEFING_URL%/}/archive/${TODAY_DATE}.html"
if [ "$DEPLOY_OK" = true ]; then
    send_tg "<b>AI 早报 · ${TODAY}</b>

今日共收录 ${ARTICLE_COUNT} 条 AI 资讯
${NO_LLM:+LLM 服务不可用，本次跳过了深度分析
}${HEALTH_WARNING:+${HEALTH_WARNING}
}
<a href=\"${BRIEFING_URL}\">点击阅读今日早报</a>
<a href=\"${ARCHIVE_URL}\">查看归档版本</a>"
else
    send_tg "<b>AI 早报 · ${TODAY}</b>

已抓取 ${ARTICLE_COUNT} 条资讯，但部署失败
页面未更新，请检查 git 配置"
fi

echo "每日任务完成" >> "$LOG_FILE"
