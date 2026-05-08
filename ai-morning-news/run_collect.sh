#!/bin/bash
# ============================================================
# AI Morning Briefing - 采集定时任务
# 由 launchd 每 2 小时自动调用，或手动运行
# 职责：RSS 抓取 → 去重 → LLM 分析 → 写入事件库
# ============================================================

set -e

# ────────────────────────────────────────────────
# Python 版本锁定 + 启动预检（与 run_daily.sh 同策略）
# ────────────────────────────────────────────────
if [ -x "/opt/anaconda3/bin/python3" ]; then
    PYTHON="/opt/anaconda3/bin/python3"
elif [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON="/opt/homebrew/bin/python3"
else
    PYTHON="python3"
fi
export PYTHON
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    _V=$("$PYTHON" -c 'import sys; print(sys.version)' 2>&1 | head -1)
    echo "$(date '+%Y-%m-%d %H:%M:%S') 🚨 PYTHON VERSION CHECK FAILED: $PYTHON = $_V (need >=3.10)" \
        >> "$(cd "$(dirname "$0")" && pwd)/collect.log"
    exit 1
fi

# ────────────────────────────────────────────────
# 互斥锁
# ────────────────────────────────────────────────
_SCRIPT_DIR_EARLY="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$_SCRIPT_DIR_EARLY/.run_collect.pid"
LOG_FILE="$_SCRIPT_DIR_EARLY/collect.log"

if [ -f "$PIDFILE" ]; then
    _OLD_PID="$(cat "$PIDFILE" 2>/dev/null || echo)"
    if [ -n "$_OLD_PID" ] && kill -0 "$_OLD_PID" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️ 另一个采集实例 (PID $_OLD_PID) 仍在运行，跳过" >> "$LOG_FILE"
        exit 0
    fi
    rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# ────────────────────────────────────────────────
# 日志轮转（1MB, 保留 5 份）
# ────────────────────────────────────────────────
LOG_MAX_SIZE=1048576
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$LOG_SIZE" -gt "$LOG_MAX_SIZE" ] 2>/dev/null; then
        for i in 4 3 2 1; do
            [ -f "${LOG_FILE}.$i" ] && mv "${LOG_FILE}.$i" "${LOG_FILE}.$((i+1))"
        done
        mv "$LOG_FILE" "${LOG_FILE}.1"
    fi
fi

{
    echo ""
    echo "──── 采集: $(date '+%Y-%m-%d %H:%M:%S') ────"
} >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 加载环境变量
ENV_FILE="$HOME/.config/ai-briefing/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# ────────────────────────────────────────────────
# LLM 服务检测
# ────────────────────────────────────────────────
NO_LLM=""
PROXY_PID=""
PROXY_STARTED_BY_US=false

LLM_URL=$("$PYTHON" -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://localhost:3456/v1")

if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    echo "  LLM 服务已在运行" >> "$LOG_FILE"
else
    # 尝试启动代理
    if [ -f "$PROJECT_DIR/claude_proxy.py" ]; then
        echo "  启动 claude_proxy.py..." >> "$LOG_FILE"
        "$PYTHON" -u "$PROJECT_DIR/claude_proxy.py" >> "$LOG_FILE" 2>&1 &
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
            echo "  代理启动超时，--no-llm" >> "$LOG_FILE"
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
        kill "$PROXY_PID" 2>/dev/null || true
        wait "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup_proxy EXIT

# ────────────────────────────────────────────────
# 运行采集器
# ────────────────────────────────────────────────
echo "开始采集..." >> "$LOG_FILE"
if "$PYTHON" -u "$PROJECT_DIR/collector.py" ${NO_LLM:-} >> "$LOG_FILE" 2>&1; then
    echo "  采集完成" >> "$LOG_FILE"
else
    echo "  ❌ 采集失败（退出码: $?）" >> "$LOG_FILE"
fi
