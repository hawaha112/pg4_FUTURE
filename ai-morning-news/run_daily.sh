#!/bin/bash
# ============================================================
# AI Morning Briefing - 每日出报 + 部署脚本
# 由 launchd 在每天早上 7:00 自动调用
#
# v4 架构：采集（collector.py / run_collect.sh）已独立为每 2 小时
# 定时任务，本脚本只负责：
#   1. 触发一次最终采集（确保出报前数据最新）
#   2. 从事件库渲染 HTML
#   3. 部署到 GitHub Pages
#   4. 发送 Telegram 通知
# ============================================================

set -e

# ────────────────────────────────────────────────
# 互斥锁
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
    rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
LOG_FILE="$PROJECT_DIR/daily_run.log"

# ────────────────────────────────────────────────
# 日志轮转（1MB, 保留 7 份）
# ────────────────────────────────────────────────
LOG_MAX_SIZE=1048576
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$LOG_SIZE" -gt "$LOG_MAX_SIZE" ] 2>/dev/null; then
        for i in 6 5 4 3 2 1; do
            [ -f "${LOG_FILE}.$i" ] && mv "${LOG_FILE}.$i" "${LOG_FILE}.$((i+1))"
        done
        mv "$LOG_FILE" "${LOG_FILE}.1"
    fi
fi

find "$PROJECT_DIR" -maxdepth 1 -name "daily_run.log.*-*" -mtime +7 -delete 2>/dev/null || true

# 加载环境变量
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# Telegram 发送函数
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

{
    echo ""
    echo "========================================"
    echo "出报时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================"
} >> "$LOG_FILE"

cd "$PROJECT_DIR"

# ────────────────────────────────────────────────
# 第一步：出报前做一次最终采集（确保事件库最新）
# ────────────────────────────────────────────────
PROXY_PID=""
PROXY_STARTED_BY_US=false

LLM_URL=$(python3 -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://localhost:3456/v1")
echo "检查 LLM 服务 ($LLM_URL)..." >> "$LOG_FILE"

NO_LLM=""
if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    echo "  LLM 服务已在运行" >> "$LOG_FILE"
else
    if [ -f "$PROJECT_DIR/claude_proxy.py" ]; then
        echo "  启动 claude_proxy.py..." >> "$LOG_FILE"
        python3 -u "$PROJECT_DIR/claude_proxy.py" >> "$LOG_FILE" 2>&1 &
        PROXY_PID=$!
        PROXY_STARTED_BY_US=true

        for i in $(seq 1 30); do
            if curl -s --connect-timeout 2 "http://localhost:3456/health" > /dev/null 2>&1; then
                echo "  代理就绪（${i}s）" >> "$LOG_FILE"
                break
            fi
            sleep 1
        done

        if ! curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
            echo "  代理启动超时，使用 --no-llm" >> "$LOG_FILE"
            NO_LLM="--no-llm"
            if [ -n "$PROXY_PID" ]; then
                kill "$PROXY_PID" 2>/dev/null || true
                PROXY_PID=""
            fi
        fi
    else
        NO_LLM="--no-llm"
    fi
fi

cleanup_proxy() {
    if [ "$PROXY_STARTED_BY_US" = true ] && [ -n "$PROXY_PID" ]; then
        echo "关闭代理 (PID: $PROXY_PID)..." >> "$LOG_FILE"
        kill "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup_proxy EXIT

echo "运行出报前采集..." >> "$LOG_FILE"
python3 -u "$PROJECT_DIR/collector.py" ${NO_LLM:-} >> "$LOG_FILE" 2>&1 || {
    echo "  ⚠️ 出报前采集失败，使用事件库中现有数据继续出报" >> "$LOG_FILE"
}

# ────────────────────────────────────────────────
# 第二步：从事件库渲染页面
# ────────────────────────────────────────────────
echo "开始渲染页面..." >> "$LOG_FILE"
if ! python3 -u "$PROJECT_DIR/briefing_renderer.py" >> "$LOG_FILE" 2>&1; then
    echo "  ❌ 渲染失败" >> "$LOG_FILE"
    send_tg "<b>AI 早报渲染失败</b>
请检查 daily_run.log"
    exit 1
fi
echo "  页面渲染完成" >> "$LOG_FILE"

# ────────────────────────────────────────────────
# 第三步：归档
# ────────────────────────────────────────────────
ARCHIVE_DIR="$PROJECT_DIR/output/archive"
TODAY_DATE=$(date '+%Y-%m-%d')
mkdir -p "$ARCHIVE_DIR"
if [ -f "$PROJECT_DIR/output/index.html" ]; then
    cp "$PROJECT_DIR/output/index.html" "$ARCHIVE_DIR/${TODAY_DATE}.html"
    cp "$PROJECT_DIR/output/modal_data.js" "$ARCHIVE_DIR/${TODAY_DATE}_modal.js" 2>/dev/null || true
    echo "  已归档: archive/${TODAY_DATE}.html" >> "$LOG_FILE"
fi

# 事件库滚动备份（保留最近 7 份），在渲染成功后才备份，保证备份是"可用的快照"
BACKUP_DATE=$(date '+%Y%m%d')
for DB in events.db dedup.db llm_cache.db; do
    SRC="$PROJECT_DIR/$DB"
    if [ -f "$SRC" ]; then
        cp "$SRC" "$PROJECT_DIR/${DB}.bak.${BACKUP_DATE}" 2>>"$LOG_FILE" || true
    fi
done
# 清理 7 天前的备份
find "$PROJECT_DIR" -maxdepth 1 -name "*.db.bak.*" -mtime +7 -delete 2>/dev/null || true

# 从 stats.json 读取文章数量与 LLM 覆盖率
STATS_FILE="$PROJECT_DIR/output/stats.json"
if [ -f "$STATS_FILE" ]; then
    ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('$STATS_FILE'))['article_count'])" 2>/dev/null || echo "?")
    LLM_COVERAGE=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('llm_coverage', ''))" 2>/dev/null || echo "")
    LLM_COUNT=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('llm_count', ''))" 2>/dev/null || echo "")
    MULTI_SRC_COUNT=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('multi_source_count', ''))" 2>/dev/null || echo "")
else
    ARTICLE_COUNT="?"
    LLM_COVERAGE=""
    LLM_COUNT=""
    MULTI_SRC_COUNT=""
fi

HEALTH_WARNING=""
if [ "$ARTICLE_COUNT" != "?" ] && [ "$ARTICLE_COUNT" -lt 3 ] 2>/dev/null; then
    HEALTH_WARNING="文章数异常偏低（仅 ${ARTICLE_COUNT} 条）"
fi

# LLM 覆盖率告警（< 50% 时）
LLM_COVERAGE_LINE=""
LLM_COVERAGE_WARNING=""
if [ -n "$LLM_COVERAGE" ]; then
    COVERAGE_PCT=$(python3 -c "print(int(round(float('$LLM_COVERAGE') * 100)))" 2>/dev/null || echo "")
    if [ -n "$COVERAGE_PCT" ]; then
        LLM_COVERAGE_LINE="LLM 深度分析覆盖率: ${COVERAGE_PCT}% (${LLM_COUNT} / ${ARTICLE_COUNT})"
        # < 50% 触发告警
        if [ "$COVERAGE_PCT" -lt 50 ] 2>/dev/null; then
            LLM_COVERAGE_WARNING="⚠️ LLM 覆盖率偏低（${COVERAGE_PCT}%），其余为规则兜底"
        fi
    fi
fi

# ────────────────────────────────────────────────
# 第四步：部署到 GitHub Pages
# ────────────────────────────────────────────────
DEPLOY_OK=false
echo "开始部署..." >> "$LOG_FILE"

DEPLOY_TMP="/tmp/ai_briefing_deploy_$$"
REPO_URL=$(cd "$PROJECT_DIR/output" && git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REPO_URL" ]; then
    REPO_URL="${DEPLOY_REPO_URL:-}"
fi

# 自动修复 output/.git 的损坏状态
if [ -d "$PROJECT_DIR/output/.git/rebase-merge" ] || [ -f "$PROJECT_DIR/output/.git/index.lock" ]; then
    echo "  ⚠️ 修复 output/.git 状态..." >> "$LOG_FILE"
    rm -f "$PROJECT_DIR/output/.git/index.lock" 2>/dev/null || true
    rm -rf "$PROJECT_DIR/output/.git/rebase-merge" 2>/dev/null || true
    cd "$PROJECT_DIR/output" && git rebase --abort 2>/dev/null || true
    cd "$PROJECT_DIR"
fi

if [ -n "$REPO_URL" ] && [ -f "$PROJECT_DIR/output/index.html" ]; then
    rm -rf "$DEPLOY_TMP"
    if git clone --depth 1 "$REPO_URL" "$DEPLOY_TMP" >> "$LOG_FILE" 2>&1; then
        cp "$PROJECT_DIR/output/index.html" "$DEPLOY_TMP/"
        cp "$PROJECT_DIR/output/modal_data.js" "$DEPLOY_TMP/" 2>/dev/null || true
        cp "$PROJECT_DIR/output/stats.json" "$DEPLOY_TMP/" 2>/dev/null || true
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
            echo "  无新变更" >> "$LOG_FILE"
            DEPLOY_OK=true
        fi
        cd "$PROJECT_DIR"
        rm -rf "$DEPLOY_TMP"
    else
        echo "  克隆远程仓库失败" >> "$LOG_FILE"
    fi
else
    echo "  跳过部署（无 git 配置或 index.html 不存在）" >> "$LOG_FILE"
fi

# ────────────────────────────────────────────────
# 第五步：Telegram 通知
# ────────────────────────────────────────────────
TODAY=$(date '+%Y年%m月%d日')
BRIEFING_URL="${BRIEFING_URL:-}"
ARCHIVE_URL="${BRIEFING_URL%/}/archive/${TODAY_DATE}.html"
if [ "$DEPLOY_OK" = true ]; then
    send_tg "<b>AI 早报 · ${TODAY}</b>

今日共收录 ${ARTICLE_COUNT} 条 AI 资讯
${LLM_COVERAGE_LINE:+${LLM_COVERAGE_LINE}
}${MULTI_SRC_COUNT:+多源交叉确认: ${MULTI_SRC_COUNT} 个事件
}${NO_LLM:+LLM 服务不可用，本次跳过了深度分析
}${LLM_COVERAGE_WARNING:+${LLM_COVERAGE_WARNING}
}${HEALTH_WARNING:+${HEALTH_WARNING}
}
<a href=\"${BRIEFING_URL}\">点击阅读今日早报</a>
<a href=\"${ARCHIVE_URL}\">查看归档版本</a>"
else
    send_tg "<b>AI 早报 · ${TODAY}</b>

已渲染 ${ARTICLE_COUNT} 条资讯，但部署失败
页面未更新，请检查 git 配置"
fi

# ────────────────────────────────────────────────
# 结构化质量摘要（machine-readable，可被监控脚本 tail -n1 | jq 消费）
# ────────────────────────────────────────────────
SUMMARY_JSON=$(python3 -c "
import json, os
from datetime import datetime, timezone
stats_path = '$STATS_FILE'
health_path = '$PROJECT_DIR/source_health.json'
stats = {}
if os.path.exists(stats_path):
    try:
        stats = json.load(open(stats_path, 'r', encoding='utf-8'))
    except Exception:
        pass
health = {}
if os.path.exists(health_path):
    try:
        health = json.load(open(health_path, 'r', encoding='utf-8'))
    except Exception:
        pass
ok = sum(1 for _, v in health.items() if v.get('status') == 'ok')
failing = sum(1 for _, v in health.items() if v.get('consecutive_failures', 0) >= 3)
dead = [n for n, v in health.items() if v.get('consecutive_failures', 0) >= 10][:5]
summary = {
    'run_id': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'kept': stats.get('article_count', 0),
    'llm_coverage': stats.get('llm_coverage'),
    'llm_count': stats.get('llm_count'),
    'multi_source_count': stats.get('multi_source_count'),
    'sources_healthy': ok,
    'sources_failing': failing,
    'dead_sources': dead,
    'deploy_ok': '$DEPLOY_OK' == 'true',
    'llm_available': '$NO_LLM' == '',
}
print('RUN_SUMMARY ' + json.dumps(summary, ensure_ascii=False))
" 2>/dev/null || echo "RUN_SUMMARY {}")
echo "$SUMMARY_JSON" >> "$LOG_FILE"

echo "每日出报任务完成" >> "$LOG_FILE"
