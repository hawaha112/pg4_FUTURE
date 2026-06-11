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
# Python 版本锁定 + 启动预检
# ────────────────────────────────────────────────
# 历史问题：launchd 环境 PATH 按 plist 解析，/usr/local/bin/python3 可能是 3.9，
# 导致 claude_proxy.py (用 PEP 604 `dict | None`) 启动即崩，流水线 2 小时空转。
# 锁到 Anaconda 的 3.12（若不存在则回退到 /opt/homebrew/bin/python3），然后严格版本检查。
if [ -x "/opt/anaconda3/bin/python3" ]; then
    PYTHON="/opt/anaconda3/bin/python3"
elif [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON="/opt/homebrew/bin/python3"
else
    PYTHON="python3"
fi
export PYTHON

_PYVER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>&1)
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    # 版本过低 → 发 TG 告警后终止（绝不静默跑空转）
    ENV_FILE_EARLY="$HOME/.config/ai-briefing/.env"
    [ -f "$ENV_FILE_EARLY" ] && . "$ENV_FILE_EARLY"
    if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\":\"${TG_CHAT_ID}\",\"text\":\"🚨 AI 早报启动失败：Python 版本 ${_PYVER} < 3.10（锁定路径 ${PYTHON}）\",\"parse_mode\":\"HTML\"}" \
            > /dev/null 2>&1 || true
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') 🚨 PYTHON VERSION CHECK FAILED: $PYTHON = $_PYVER (need >=3.10)" \
        >> "$(cd "$(dirname "$0")" && pwd)/daily_run.log"
    exit 1
fi

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
_RUN_START_TS=$(date +%s)

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
# 当 BRIEFING_SILENT_TG=true 时跳过推送 — 测试 dispatch 时避免轰炸 TG。
# 构建 sendMessage JSON payload。给了 url 就把"裸 URL 纯文本"附在正文末尾。
# ⚠️ TG"Open Link?"二次确认是反钓鱼: 只要"可见文字 ≠ 真实 URL"就弹 ——
#    正文 <a href>(文字≠URL)、inline 按钮(按钮文字≠URL)在频道里都会触发确认。
#    唯一一点直达的是裸 URL 纯文本(显示的就是 URL 本身、无伪装), TG 自动识别、点了直接开。
#    (web_app 按钮能免确认, 但仅私聊可用; 本项目推到频道, 用不了。)
_tg_payload() {
    # $1=text  $2=url(可空)  $3=链接前的标签文字(可空)
    TG_TEXT="$1" TG_URL="${2:-}" TG_BTN="${3:-📖 阅读全文}" TG_CHAT="$TG_CHAT_ID" \
    "$PYTHON" -c '
import os, json
text = os.environ.get("TG_TEXT", "")
u = (os.environ.get("TG_URL", "") or "").strip()
if u:
    label = (os.environ.get("TG_BTN", "") or "").strip()
    # 标签是纯文本(不伪装链接); URL 单独成行、保持裸文本 → 一点直达不弹确认
    text = text + "\n\n" + (label + "：\n" if label else "") + u
p = {"chat_id": os.environ.get("TG_CHAT", ""), "text": text,
     "parse_mode": "HTML", "disable_web_page_preview": True}
print(json.dumps(p, ensure_ascii=False))
'
}

send_tg() {
    if [ "${BRIEFING_SILENT_TG:-false}" = "true" ]; then
        echo "  🔇 silent_tg=true, 跳过 TG 推送" >> "$LOG_FILE"
        return 0
    fi
    if [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
        return 0
    fi
    # $1=message  $2=url(可选→附按钮)  $3=按钮文字(可选)
    local payload
    payload=$(_tg_payload "$1" "${2:-}" "${3:-}")
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" -d "$payload" \
        > /dev/null 2>&1 || true
}

# 早报"单条更新"状态文件：存上一条早报消息的 message_id，云端跑由 briefing-state 持久化。
TG_STATE_FILE="$PROJECT_DIR/tg_state.json"

# 删除一条历史 TG 消息（best-effort）。Telegram 允许 bot 删自己 <48h 的消息，
# 早报每 12h 一班、上一条始终在窗口内，删得掉。删失败不致命（|| true）。
tg_delete() {
    local msg_id="$1"
    [ -z "$msg_id" ] && return 0
    [ "${BRIEFING_SILENT_TG:-false}" = "true" ] && return 0
    { [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; } && return 0
    curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/deleteMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": \"${TG_CHAT_ID}\", \"message_id\": ${msg_id}}" \
        > /dev/null 2>&1 || true
}

# 发送一条消息并把新的 message_id 打到 stdout（失败/silent 时输出空）。
# 用于"早报单条更新"：拿到 id 才能在下一班删掉它。
send_tg_capture() {
    if [ "${BRIEFING_SILENT_TG:-false}" = "true" ]; then
        echo "  🔇 silent_tg=true, 跳过 TG 推送" >> "$LOG_FILE"
        return 0
    fi
    if [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
        return 0
    fi
    # $1=message  $2=url(可选→附 inline 按钮, 一点直达不弹确认)  $3=按钮文字(可选)
    local payload resp
    payload=$(_tg_payload "$1" "${2:-}" "${3:-}")
    # || resp="" 兜底: set -e 下 curl 网络失败会让赋值返回非零 → 整脚本在已部署后崩退
    resp=$(curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" -d "$payload" \
        2>/dev/null) || resp=""
    printf '%s' "$resp" | "$PYTHON" -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d.get("result",{}).get("message_id","") if d.get("ok") else "")
except Exception:
    print("")' 2>/dev/null || true
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

LLM_URL=$("$PYTHON" -c "import json; print(json.load(open('config.json'))['llm']['base_url'])" 2>/dev/null || echo "http://localhost:3456/v1")
echo "检查 LLM 服务 ($LLM_URL)..." >> "$LOG_FILE"

NO_LLM=""
if curl -s --connect-timeout 5 "${LLM_URL}/models" > /dev/null 2>&1; then
    echo "  LLM 服务已在运行" >> "$LOG_FILE"
else
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
            # 代理启动失败 → 发 TG 告警并退出，绝不静默降级跑空转
            # （Python 版本预检已保证 3.10+，代理仍挂说明 claude CLI / keychain / 代码真故障）
            echo "  ❌ 代理启动失败，终止流程（不再静默 --no-llm）" >> "$LOG_FILE"
            if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
                _ERR_TAIL=$(tail -30 "$LOG_FILE" 2>/dev/null | grep -E "Error|Traceback|line [0-9]" | tail -5 | sed 's/"/\\"/g' | tr '\n' ' ' | cut -c1-400)
                curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
                    -H "Content-Type: application/json" \
                    -d "{\"chat_id\":\"${TG_CHAT_ID}\",\"text\":\"🚨 AI 早报启动失败: claude_proxy 30s 内无响应\\n\\n最近错误:\\n${_ERR_TAIL}\\n\\n请检查 claude auth / daily_run.log\",\"parse_mode\":\"HTML\"}" \
                    > /dev/null 2>&1 || true
            fi
            if [ -n "$PROXY_PID" ]; then
                kill "$PROXY_PID" 2>/dev/null || true
                PROXY_PID=""
            fi
            exit 1
        fi

        # ────────────────────────────────────────────────
        # token 真打探测：proxy /health 起来不代表 OAuth token 有效
        # 历史问题：token 失效时 proxy 启动正常，每篇 LLM 调用 401 返 500，
        # 流水线静默跑完 → kept=0 → 跳过部署 → TG 收到的是"部署失败"而非
        # 根因告警，要 download artifact 才能看到 401。改成开跑前先打一发。
        #
        # 判定逻辑用白名单：成功响应必含 OpenAI 标准字段 "choices"。
        # 旧版用 grep '401|invalid|failed' 黑名单，结果命中合法响应里
        # `chatcmpl-31722be276284010b26f60be` 的 4010 子串导致误杀。
        # ────────────────────────────────────────────────
        echo "  验证 LLM token..." >> "$LOG_FILE"
        PROBE_RESP=$(curl -s --max-time 60 -X POST "${LLM_URL}/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"claude-sonnet-4","messages":[{"role":"user","content":"ping"}],"max_tokens":4}' 2>&1)
        if echo "$PROBE_RESP" | grep -q '"choices"'; then
            echo "  ✅ LLM token 验证通过" >> "$LOG_FILE"
        else
            # 没有 choices → 不是正常 chat-completions 响应。区分 token 失败和其他故障
            if echo "$PROBE_RESP" | grep -qiE 'failed to authenticate|invalid authentication|invalid api key'; then
                _ERR_TYPE="token"
                _TG_TEXT="🚨 AI 早报启动失败: <b>CLAUDE_CODE_OAUTH_TOKEN 已失效 (401)</b>\\n\\n本地跑 <code>claude setup-token</code> 重新生成, 然后:\\n<code>gh secret set CLAUDE_CODE_OAUTH_TOKEN -R hawaha112/pg4_FUTURE</code>"
            else
                _ERR_TYPE="proxy"
                _TG_TEXT="🚨 AI 早报启动失败: claude_proxy 探测无 \\\"choices\\\" 响应\\n\\n响应前 200 字: $(echo "$PROBE_RESP" | tr '\n\"' '  ' | cut -c1-200)"
            fi
            echo "  ❌ LLM 探测失败 ($_ERR_TYPE), 终止流程" >> "$LOG_FILE"
            echo "  探测响应: $(echo "$PROBE_RESP" | tr '\n' ' ' | cut -c1-300)" >> "$LOG_FILE"
            if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
                curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
                    -H "Content-Type: application/json" \
                    -d "{\"chat_id\":\"${TG_CHAT_ID}\",\"text\":\"${_TG_TEXT}\",\"parse_mode\":\"HTML\"}" \
                    > /dev/null 2>&1 || true
            fi
            kill "$PROXY_PID" 2>/dev/null || true
            exit 1
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
"$PYTHON" -u "$PROJECT_DIR/collector.py" ${NO_LLM:-} >> "$LOG_FILE" 2>&1 || {
    echo "  ⚠️ 出报前采集失败，使用事件库中现有数据继续出报" >> "$LOG_FILE"
}

# ────────────────────────────────────────────────
# 第二步：从事件库渲染页面
# ────────────────────────────────────────────────
# 早/晚班：按当前小时判断（06:00 触发 = 早班，覆盖前夜 18:00→今晨 06:00）
# 调试时可通过外部环境变量 BRIEFING_SHIFT_OVERRIDE=am|pm 强制覆盖。
CURRENT_HOUR=$(date '+%H' | sed 's/^0//')
if [ -n "${BRIEFING_SHIFT_OVERRIDE:-}" ]; then
    SHIFT="$BRIEFING_SHIFT_OVERRIDE"
    SHIFT_LABEL="🔧 强制 $SHIFT 班"
elif [ "${CURRENT_HOUR:-0}" -lt 12 ] 2>/dev/null; then
    SHIFT="am"
    SHIFT_LABEL="🌅 早班 (前夜→今晨)"
else
    SHIFT="pm"
    SHIFT_LABEL="🌆 晚班 (今晨→今晚)"
fi
export BRIEFING_SHIFT="$SHIFT"
echo "班次: $SHIFT_LABEL" >> "$LOG_FILE"

echo "开始渲染页面..." >> "$LOG_FILE"
if ! "$PYTHON" -u "$PROJECT_DIR/briefing_renderer.py" --hours 12 >> "$LOG_FILE" 2>&1; then
    echo "  ❌ 渲染失败" >> "$LOG_FILE"
    send_tg "<b>AI 早报渲染失败</b>
请检查 daily_run.log"
    exit 1
fi
echo "  页面渲染完成" >> "$LOG_FILE"

# 生成跑步健康仪表盘（读 output/run_health.jsonl，含过往跑次趋势）
# 本次 RUN_SUMMARY 在脚本末尾才写 jsonl，所以 dashboard 反映的是"上次及以前"的记录；
# 下次跑时本次就会进 dashboard。这个延迟 1 次的折中使部署流程更简单。
"$PYTHON" -u "$PROJECT_DIR/dashboard_generator.py" >> "$LOG_FILE" 2>&1 || \
    echo "  ⚠️ dashboard 生成失败，跳过" >> "$LOG_FILE"
# 同时放一份到 output/archive/（走 workflow 白名单 archive/** 部署，避免改 deploy.yml）
# dashboard 默认实体链接是 ../entities/ (适配 archive/dashboard.html 视角).
# 根目录 dashboard.html 视角下要去掉 .. — sed 替换 ../entities/ → entities/.
if [ -f "$PROJECT_DIR/output/dashboard.html" ]; then
    mkdir -p "$PROJECT_DIR/output/archive"
    # archive 版: 默认相对路径正确, 直接 cp
    cp "$PROJECT_DIR/output/dashboard.html" "$PROJECT_DIR/output/archive/dashboard.html"
    # 根目录版: 修正实体链接 ../entities/ → entities/
    sed -i.bak 's|href="\.\./entities/|href="entities/|g' "$PROJECT_DIR/output/dashboard.html"
    rm -f "$PROJECT_DIR/output/dashboard.html.bak"
fi

# ────────────────────────────────────────────────
# 第三步：归档
# ────────────────────────────────────────────────
ARCHIVE_DIR="$PROJECT_DIR/output/archive"
TODAY_DATE=$(date '+%Y-%m-%d')
mkdir -p "$ARCHIVE_DIR"
if [ -f "$PROJECT_DIR/output/index.html" ]; then
    # 归档时把 HTML 里硬编码的 'modal_data.js' 替换成本班次专属文件名，
    # 否则 archive/ 下找不到 modal_data.js 导致点击卡片无反应（404）。
    # 同时把仪表盘链接 'archive/dashboard.html' 改回 'dashboard.html' —
    # 主页用前者（GitHub Pages workflow 的白名单不含根目录 dashboard.html），
    # 归档页本身就在 archive/ 里，链接直接相对到同目录的 dashboard.html 即可。
    # 实体追踪 chip 的 href="entities/..." 是相对主页（根目录）的；归档页在 archive/ 下，
    # 同样的相对链接会解析成 archive/entities/... → 404。改写成 ../entities/... 回到根目录。
    # 音频播放器 src="archive/audio/..." 是相对主页的; 归档页在 archive/ 下要去掉前缀。
    sed -e "s|s\.src = 'modal_data\.js'|s.src = '${TODAY_DATE}-${SHIFT}_modal.js'|" \
        -e 's|href="archive/dashboard\.html"|href="dashboard.html"|g' \
        -e 's|href="entities/|href="../entities/|g' \
        -e 's|src="archive/audio/|src="audio/|g' \
        "$PROJECT_DIR/output/index.html" > "$ARCHIVE_DIR/${TODAY_DATE}-${SHIFT}.html"
    cp "$PROJECT_DIR/output/modal_data.js" "$ARCHIVE_DIR/${TODAY_DATE}-${SHIFT}_modal.js" 2>/dev/null || true
    echo "  已归档: archive/${TODAY_DATE}-${SHIFT}.html" >> "$LOG_FILE"
fi

# ────────────────────────────────────────────────
# 第三步半：口播音频 (5-6 分钟 TTS+背景乐, 增值件, 失败绝不挡出报)
# 产物 output/archive/audio/${TODAY_DATE}-${SHIFT}.mp3 — 部署块 cp -r archive/* 自动带上,
# 页面 <audio> 已按此约定引用; TG 推送块在早报消息后随发同一文件。
# ────────────────────────────────────────────────
if [ -f "$PROJECT_DIR/output/broadcast.txt" ]; then
    echo "🎙 合成口播音频..." >> "$LOG_FILE"
    "$PYTHON" "$PROJECT_DIR/tts_broadcast.py" "archive/audio/${TODAY_DATE}-${SHIFT}.mp3" >> "$LOG_FILE" 2>&1 || true
fi

# 事件库滚动备份（保留最近 7 份），在渲染成功后才备份，保证备份是"可用的快照"
BACKUP_DATE=$(date '+%Y%m%d')
for DB in events.db dedup.db llm_cache.db; do
    SRC="$PROJECT_DIR/$DB"
    if [ -f "$SRC" ]; then
        cp "$SRC" "$PROJECT_DIR/${DB}.bak.${BACKUP_DATE}" 2>>"$LOG_FILE" || true
    fi
done
# 清理 7 天前的备份（含 db 滚动备份、历史修复前快照、损坏 db 文件）
find "$PROJECT_DIR" -maxdepth 1 -type f \( \
    -name "*.db.bak.*" -o \
    -name "*.db.before-fix-*" -o \
    -name "*.db.bak" -o \
    -name "*.corrupt" \
    \) -mtime +7 -delete 2>/dev/null || true

# 异地备份到 iCloud Drive（防本地磁盘事故全丢）— 仅 macOS
# events.db 是事件主库；dedup.db 丢了重建会让历史去重失效（会出重复卡）；
# llm_cache.db 丢了重建会重新调 LLM 浪费 token，所以三个都备。
if [[ "$OSTYPE" == "darwin"* ]]; then
    ICLOUD_BACKUP_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/AI早报备份"
    if [ -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs" ]; then
        mkdir -p "$ICLOUD_BACKUP_DIR"
        for DB in events.db dedup.db llm_cache.db; do
            [ -f "$PROJECT_DIR/$DB" ] && \
                cp "$PROJECT_DIR/$DB" "$ICLOUD_BACKUP_DIR/${DB}.bak.${BACKUP_DATE}" 2>>"$LOG_FILE" || true
        done
        # iCloud 端保留 14 份（半月，比本地宽松）
        find "$ICLOUD_BACKUP_DIR" -maxdepth 1 \( \
            -name "events.db.bak.*" -o \
            -name "dedup.db.bak.*" -o \
            -name "llm_cache.db.bak.*" \
            \) -mtime +14 -delete 2>/dev/null || true
        echo "  ☁️  iCloud 备份: events.db / dedup.db / llm_cache.db (.${BACKUP_DATE})" >> "$LOG_FILE"
    fi
fi

# 从 stats.json 读取文章数量与 LLM 覆盖率
STATS_FILE="$PROJECT_DIR/output/stats.json"
if [ -f "$STATS_FILE" ]; then
    ARTICLE_COUNT=$("$PYTHON" -c "import json; print(json.load(open('$STATS_FILE'))['article_count'])" 2>/dev/null || echo "?")
    LLM_COVERAGE=$("$PYTHON" -c "import json; print(json.load(open('$STATS_FILE')).get('llm_coverage', ''))" 2>/dev/null || echo "")
    LLM_COUNT=$("$PYTHON" -c "import json; print(json.load(open('$STATS_FILE')).get('llm_count', ''))" 2>/dev/null || echo "")
    MULTI_SRC_COUNT=$("$PYTHON" -c "import json; print(json.load(open('$STATS_FILE')).get('multi_source_count', ''))" 2>/dev/null || echo "")
    IMP_COUNT=$("$PYTHON" -c "import json; print(json.load(open('$STATS_FILE')).get('important_count', 0))" 2>/dev/null || echo "0")
else
    ARTICLE_COUNT="?"
    LLM_COVERAGE=""
    LLM_COUNT=""
    MULTI_SRC_COUNT=""
    IMP_COUNT="0"
fi

HEALTH_WARNING=""
if [ "$ARTICLE_COUNT" != "?" ] && [ "$ARTICLE_COUNT" -lt 3 ] 2>/dev/null; then
    HEALTH_WARNING="文章数异常偏低（仅 ${ARTICLE_COUNT} 条）"
fi

# LLM 覆盖率告警（< 50% 时）
LLM_COVERAGE_LINE=""
LLM_COVERAGE_WARNING=""
if [ -n "$LLM_COVERAGE" ]; then
    COVERAGE_PCT=$("$PYTHON" -c "print(int(round(float('$LLM_COVERAGE') * 100)))" 2>/dev/null || echo "")
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
DEPLOY_REPO="${DEPLOY_REPO:-hawaha112/ai-morning-briefing}"
DEPLOY_SSH_KEYFILE=""
# REPO_URL 优先级：
#   1. DEPLOY_SSH_KEY env（推荐, SSH deploy key, 永不过期 → 无需续 token） → SSH URL + GIT_SSH_COMMAND
#   2. DEPLOY_REPO_TOKEN env（兜底, PAT, 会过期；daily-ops 会临期告警） → PAT 认证 URL
#   3. output/.git remote（Mac 本地，已 clone 过部署仓库）
#   4. DEPLOY_REPO_URL env（手动覆盖）
if [ -n "${DEPLOY_SSH_KEY:-}" ]; then
    DEPLOY_SSH_KEYFILE="$(mktemp)"
    printf '%s\n' "${DEPLOY_SSH_KEY}" > "$DEPLOY_SSH_KEYFILE"
    chmod 600 "$DEPLOY_SSH_KEYFILE"
    # IdentitiesOnly: 只用这把 key; accept-new: 首次自动信任 github.com host key (CI 无 known_hosts)
    export GIT_SSH_COMMAND="ssh -i ${DEPLOY_SSH_KEYFILE} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    REPO_URL="git@github.com:${DEPLOY_REPO}.git"
    echo "  部署认证: SSH deploy key (永不过期)" >> "$LOG_FILE"
elif [ -n "${DEPLOY_REPO_TOKEN:-}" ]; then
    REPO_URL="https://x-access-token:${DEPLOY_REPO_TOKEN}@github.com/${DEPLOY_REPO}.git"
    echo "  部署认证: PAT 兜底 (会过期)" >> "$LOG_FILE"
else
    REPO_URL=$(cd "$PROJECT_DIR/output" && git remote get-url origin 2>/dev/null || echo "")
    if [ -z "$REPO_URL" ]; then
        REPO_URL="${DEPLOY_REPO_URL:-}"
    fi
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
        cp "$PROJECT_DIR/output/dashboard.html" "$DEPLOY_TMP/" 2>/dev/null || true
        if [ -d "$PROJECT_DIR/output/archive" ]; then
            mkdir -p "$DEPLOY_TMP/archive"
            cp -r "$PROJECT_DIR/output/archive/"* "$DEPLOY_TMP/archive/" 2>/dev/null || true
        fi
        # P1: 实体时间线页 (entities/{entity}-30d.html) 也要推
        if [ -d "$PROJECT_DIR/output/entities" ]; then
            mkdir -p "$DEPLOY_TMP/entities"
            cp -r "$PROJECT_DIR/output/entities/"* "$DEPLOY_TMP/entities/" 2>/dev/null || true
        fi
        # 长期归档: 本班事件+判断 幂等追加进部署仓 archive/data/(月度 JSONL + 搜索索引
        # + 判断库 + manifest)。永久保存, 不受 events.db 30 天清理影响; 失败不挡部署。
        "$PYTHON" "$PROJECT_DIR/archive_appender.py" \
            "$PROJECT_DIR/output/archive_payload.json" \
            "$DEPLOY_TMP/archive/data" >> "$LOG_FILE" 2>&1 || true
        # HQ 离线重制任务(用户 2026-06-11 定 E=Qwen3 为最佳): 把口播文本+任务标记发布到
        # 部署仓(公开, Actions 分钟不限量), 那边的 hq-audio.yml 监听 hq_job.json 的 push,
        # 用慢但最自然的 Qwen3 引擎重制音频(~75-90 分钟)后原地覆盖 mp3(页面自动升级),
        # 再由 pg4 的 hq-tg-edit.yml 定时任务把 TG 消息的音频原地换掉。失败不影响本班。
        if [ -f "$PROJECT_DIR/output/broadcast.txt" ]; then
            mkdir -p "$DEPLOY_TMP/archive/data"
            cp "$PROJECT_DIR/output/broadcast.txt" "$DEPLOY_TMP/archive/data/broadcast-${SHIFT}.txt" 2>/dev/null || true
            BC_SHA=$(shasum "$PROJECT_DIR/output/broadcast.txt" 2>/dev/null | cut -c1-16 || echo "x")
            printf '{"date":"%s","shift":"%s","sha":"%s","audio":"archive/audio/%s-%s.mp3"}\n' \
                "$TODAY_DATE" "$SHIFT" "$BC_SHA" "$TODAY_DATE" "$SHIFT" \
                > "$DEPLOY_TMP/archive/data/hq_job.json" 2>/dev/null || true
        fi
        # 音频只留 14 天: 每班 ~3-4MB, 不清理数月就拖垮 clone/Pages 配额。
        # ⚠️ 不能用 find -mtime(fresh clone 的 mtime 全是克隆时刻) — 按文件名日期裁。
        # 老归档页的 <audio> 对被裁文件会 onerror 自动隐藏, 优雅降级。
        AUDIO_CUTOFF=$(date -u -v-14d +%F 2>/dev/null || date -u -d '14 days ago' +%F)
        for f in "$DEPLOY_TMP/archive/audio/"*.mp3; do
            [ -e "$f" ] || continue
            b=$(basename "$f")
            [ "${b:0:10}" \< "$AUDIO_CUTOFF" ] && rm -f "$f" || true
        done
        cd "$DEPLOY_TMP"
        git config user.email "hawaha113@protonmail.com"
        git config user.name "hawaha112"
        git add -A
        if ! git diff --cached --quiet; then
            git commit -m "Daily update: $(date '+%Y-%m-%d %H:%M')" >> "$LOG_FILE" 2>&1
            if git push origin main >> "$LOG_FILE" 2>&1; then
                echo "  部署成功" >> "$LOG_FILE"
                DEPLOY_OK=true
            # 周日 18:00 周报/记分牌与 pm 班可能并发推同一仓 → non-fast-forward。
            # rebase 一次重试(部署是整目录覆盖 cp + 幂等 JSONL 追加, 重放安全)。
            elif git pull --rebase origin main >> "$LOG_FILE" 2>&1 \
                 && git push origin main >> "$LOG_FILE" 2>&1; then
                echo "  部署成功 (rebase 重试)" >> "$LOG_FILE"
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
ARCHIVE_URL="${BRIEFING_URL%/}/archive/${TODAY_DATE}-${SHIFT}.html"

# 本次总用时
_RUN_END_TS=$(date +%s)
_DURATION_SEC=$(( _RUN_END_TS - _RUN_START_TS ))
_DURATION_MIN=$(( _DURATION_SEC / 60 ))
_DURATION_REM=$(( _DURATION_SEC % 60 ))
DURATION_LINE="⏱ 用时: ${_DURATION_MIN} 分 ${_DURATION_REM} 秒"

# 源健康度统计 — 同 SUMMARY_JSON 的 dead/failing 计算思路：排除 config
# 里 enabled=false 的源（已显式禁用的源不再计入死源/告警）
HEALTH_JSON="$PROJECT_DIR/source_health.json"
CONFIG_JSON="$PROJECT_DIR/config.json"
SRC_OK=0; SRC_FAIL=0; SRC_DEAD=0
if [ -f "$HEALTH_JSON" ]; then
    SRC_STATS=$("$PYTHON" -c "
import json
h = json.load(open('$HEALTH_JSON'))
disabled = set()
try:
    cfg = json.load(open('$CONFIG_JSON'))
    for lst in cfg.get('sources', {}).values():
        if isinstance(lst, list):
            for s in lst:
                if not s.get('enabled', True) or s.get('disabled', False):
                    nm = s.get('name')
                    if nm: disabled.add(nm)
except Exception:
    pass
ok = sum(1 for n, v in h.items() if v.get('status')=='ok' and n not in disabled)
fail = sum(1 for n, v in h.items()
          if 3 <= v.get('consecutive_failures', 0) < 10 and n not in disabled)
dead = sum(1 for n, v in h.items()
           if v.get('consecutive_failures', 0) >= 10 and n not in disabled)
print(f'{ok} {fail} {dead}')
" 2>/dev/null || echo "0 0 0")
    read -r SRC_OK SRC_FAIL SRC_DEAD <<< "$SRC_STATS"
fi
SRC_LINE="📡 源健康: ${SRC_OK} OK / ${SRC_FAIL} 告警 / ${SRC_DEAD} 死源"

# 读取"本班次"上一条消息 id（早报/晚报各自独立的槽 briefing_msg_id_{am,pm}，互不删除）。
# 云端跑由 briefing-state 持久化 tg_state.json。兼容旧版单键 briefing_msg_id（首次升级时回退读它）。
PREV_MSG_ID=""
if [ -f "$TG_STATE_FILE" ]; then
    PREV_MSG_ID=$("$PYTHON" -c "import json;d=json.load(open('$TG_STATE_FILE'));print(d.get('briefing_msg_id_$SHIFT') or d.get('briefing_msg_id') or '')" 2>/dev/null || echo "")
fi

if [ "$DEPLOY_OK" = true ]; then
    # 内容优先(P0): 前置 important_events 头条, 让你在 TG 一眼看到"发生了什么"、不必点进去。
    # 砍掉 LLM 覆盖率/源健康/用时等流水线诊断(那些进仪表盘, 不进每日推送)。
    # 窗口里只保留这一条, 每班"删旧→推新"刷新。
    if [ "$SHIFT" = "am" ]; then RPT="早报"; SHORT_SHIFT="早班"; else RPT="晚报"; SHORT_SHIFT="晚班"; fi
    HEADLINES=""
    [ -f "$STATS_FILE" ] && HEADLINES=$("$PYTHON" "$PROJECT_DIR/_tg_headlines.py" "$STATS_FILE" 5 2>/dev/null || echo "")
    WARN_LINE=""
    [ -n "$NO_LLM" ] && WARN_LINE=$'\n'"⚠️ LLM 暂不可用，本班为规则兜底内容"
    # 正文不放 <a href> 伪装链接 / inline 按钮(频道里都会触发 TG"Open Link?"二次确认)。
    # URL 由 _tg_payload 以"裸 URL 纯文本"附在末尾(send_tg_capture 第 2 参), 一点直达。
    BRIEFING_MSG="<b>📰 AI ${RPT} · $(date '+%m-%d') ${SHORT_SHIFT}</b>
${HEADLINES:+
${HEADLINES}
}
共 <b>${ARTICLE_COUNT}</b> 条${WARN_LINE}"
    # ── 音频嵌入早报(用户 2026-06-10): 有音频时发"一条合体消息"(sendAudio,
    #    caption=早报正文+链接) —— 每班仍只占一条消息, 点开即听、往下即读。
    #    无音频/合体失败 → 回退纯文本 sendMessage, 早报永远发得出去。 ──
    AUDIO_FILE="$PROJECT_DIR/output/archive/audio/${TODAY_DATE}-${SHIFT}.mp3"
    # 旧版"独立音频消息"槽位遗留 id 一并删除(避免双消息时代残留堆积)
    PREV_AUDIO_ID=""
    [ -f "$TG_STATE_FILE" ] && PREV_AUDIO_ID=$("$PYTHON" -c "import json;print(json.load(open('$TG_STATE_FILE')).get('audio_msg_id_${SHIFT}',''))" 2>/dev/null || echo "")
    tg_delete "$PREV_MSG_ID"
    tg_delete "$PREV_AUDIO_ID"
    NEW_MSG_ID=""
    if [ -f "$AUDIO_FILE" ] && [ "$BRIEFING_SILENT_TG" != "true" ] && [ -n "$TG_BOT_TOKEN" ]; then
        AUDIO_MIN=$("$PYTHON" -c "from mutagen.mp3 import MP3;print(f'{MP3(\"$AUDIO_FILE\").info.length/60:.0f}')" 2>/dev/null || echo "")
        # caption 上限 1024: 截到 1000(按行截断), 链接永远保留(裸 URL 一点直达)
        CAPTION=$(BMSG="$BRIEFING_MSG" BURL="$BRIEFING_URL" AMIN="$AUDIO_MIN" "$PYTHON" - <<'PYEOF' 2>>"$LOG_FILE" || printf '%s' "$BRIEFING_MSG"
import os
msg = os.environ.get('BMSG', '')
url = (os.environ.get('BURL', '') or '').strip()
amin = os.environ.get('AMIN', '')
head = ("🎧 音频版约 " + amin + " 分钟 · 文字速览👇\n\n") if amin else ""
tail = ("\n\n📖 文字版全文:\n" + url) if url else ""
limit = 1000 - len(head) - len(tail)
if len(msg) > limit:
    out, n = [], 0
    for ln in msg.split('\n'):
        if n + len(ln) + 1 > limit - 2:
            break
        out.append(ln)
        n += len(ln) + 1
    msg = '\n'.join(out) + '\n…'
print(head + msg + tail, end='')
PYEOF
)
        # ⚠️ caption 以 <b> 开头, curl -F 会把开头的 < 当"读文件"语法 → 必须 --form-string
        AUDIO_RESP=$(curl -s --max-time 120 "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendAudio" \
            -F "chat_id=${TG_CHAT_ID}" \
            -F "audio=@${AUDIO_FILE}" \
            --form-string "title=AI ${RPT} · $(date '+%m-%d') ${SHORT_SHIFT}" \
            --form-string "performer=AI Morning Briefing" \
            --form-string "caption=${CAPTION}" \
            -F "parse_mode=HTML") || AUDIO_RESP=""
        NEW_MSG_ID=$(printf '%s' "$AUDIO_RESP" | "$PYTHON" -c "import sys,json;d=json.loads(sys.stdin.read() or '{}');print((d.get('result') or {}).get('message_id','') if isinstance(d,dict) else '')" 2>/dev/null || echo "")
        if [ -n "$NEW_MSG_ID" ]; then
            echo "  🎧 合体消息(音频+早报)已推送: msg_id=${NEW_MSG_ID}" >> "$LOG_FILE"
            # 存 caption + 班次日期 → hq-tg-edit.yml 稍后用 E 引擎高质量音频原地
            # editMessageMedia 升级这条消息(caption 必须重传, 否则会被清空)
            CAPTION_ENV="$CAPTION" "$PYTHON" - "$TG_STATE_FILE" "$SHIFT" "$TODAY_DATE" <<'PYEOF' 2>>"$LOG_FILE" || true
import json, os, sys
path, shift, d = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    st = json.load(open(path)) if os.path.exists(path) else {}
except Exception:
    st = {}
if not isinstance(st, dict):
    st = {}
st['briefing_caption_' + shift] = os.environ.get('CAPTION_ENV', '')
st['briefing_msg_date_' + shift] = d
st.pop('hq_edited_' + shift, None)   # 新一班, 清上一班的"已升级"标记
json.dump(st, open(path, 'w'), ensure_ascii=False)
PYEOF
        else
            echo "  ⚠️ 合体消息失败(caption 超限/网络/限流), 回退纯文本: $(printf '%s' "$AUDIO_RESP" | head -c 160)" >> "$LOG_FILE"
        fi
    fi
    if [ -z "$NEW_MSG_ID" ]; then
        NEW_MSG_ID=$(send_tg_capture "$BRIEFING_MSG" "$BRIEFING_URL" "📖 阅读全文")
    fi
    if [ -n "$NEW_MSG_ID" ]; then
        # 只更新本班次(am/pm)的槽，保留另一班次的 id —— 早报/晚报各一条、互不顶替、各自每天刷新
        "$PYTHON" - "$TG_STATE_FILE" "$SHIFT" "$NEW_MSG_ID" <<'PYEOF' 2>>"$LOG_FILE" || \
            printf '{"briefing_msg_id_%s": %s}\n' "$SHIFT" "$NEW_MSG_ID" > "$TG_STATE_FILE"
import json, os, sys
from datetime import datetime, timezone
path, shift, mid = sys.argv[1], sys.argv[2], sys.argv[3]
d = {}
if os.path.exists(path):
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
if not isinstance(d, dict):
    d = {}
d.pop('briefing_msg_id', None)   # 清理旧版单键，迁移到 am/pm 双槽
d.pop('audio_msg_id_' + shift, None)   # 清理"独立音频消息"时代的槽(已并入合体消息)
d['briefing_msg_id_' + shift] = int(mid)
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
json.dump(d, open(path, 'w'), ensure_ascii=False)
PYEOF
        echo "  📌 ${SHIFT} 单条更新: 删旧(${PREV_MSG_ID:-无}) → 新 msg_id=${NEW_MSG_ID}（另一班次保留）" >> "$LOG_FILE"
    else
        echo "  ⚠️ 未取到新 msg_id（silent 或发送失败），保留旧 id 不变" >> "$LOG_FILE"
    fi
else
    # 部署失败：不删上一条"可用早报"（保留最后一条好链接），单独发告警。
    send_tg "<b>⚠️ ${SHIFT_LABEL} · ${TODAY}</b>

⚠️ 已渲染 ${ARTICLE_COUNT} 条但 <b>部署失败</b>，窗口里仍保留上一条可用早报。
${SRC_LINE}
${DURATION_LINE}

请检查 git 配置 / 日志"
fi

# kept=0 静默失败防御：醒目告警（无论部署成功与否都发，DEPLOY_OK=false 也常因 kept=0 触发）
if [ "${ARTICLE_COUNT:-0}" = "0" ] || [ "${ARTICLE_COUNT:-?}" = "?" ]; then
    send_tg "<b>🚨 早报告警：本班次条目数为 0</b>

班次：${SHIFT_LABEL}
部署：$([ "$DEPLOY_OK" = true ] && echo '✅' || echo '❌')
${SRC_LINE}
LLM 可用：$([ -z "$NO_LLM" ] && echo '✅' || echo '❌ (--no-llm)')

可能原因：① LLM token 失效 ② LLM 链路故障 ③ 时间窗口无新闻 ④ 聚类/质量全部过滤

请打开 daily_run.log 检查 RUN_SUMMARY 行 + 上下文"
fi

# ────────────────────────────────────────────────
# 结构化质量摘要（machine-readable，可被监控脚本 tail -n1 | jq 消费）
# 抽成独立 _run_summary.py 脚本 — bash heredoc + 多行 Python + 引用嵌套
# 在云端 runner 上偶发静默失败（stdout 空,无 stderr）。独立脚本最稳。
# ────────────────────────────────────────────────
LLM_AVAILABLE_FLAG=$([ -z "$NO_LLM" ] && echo true || echo false)
SUMMARY_JSON=$("$PYTHON" -u "$PROJECT_DIR/_run_summary.py" \
    "$STATS_FILE" \
    "$PROJECT_DIR/source_health.json" \
    "$PROJECT_DIR/config.json" \
    "$SHIFT" \
    "$_DURATION_SEC" \
    "$DEPLOY_OK" \
    "$LLM_AVAILABLE_FLAG" \
    2>>"$LOG_FILE" || echo "RUN_SUMMARY {}")
echo "$SUMMARY_JSON" >> "$LOG_FILE"
if [ -z "$SUMMARY_JSON" ]; then
    echo "RUN_SUMMARY {} (empty - _run_summary.py stdout was empty)" >> "$LOG_FILE"
fi

# ────────────────────────────────────────────────
# Dashboard 后置补部署：本次 RUN_SUMMARY 已写入 jsonl，重新生成 dashboard
# 让"最新跑"立刻可见（不再滞后 1 次）。只 push 单文件，几秒完成。
#
# 这里有两条路径 — 主部署阶段已经把 dashboard 推上去了，但那次是 jsonl
# 写本次记录之前生成的，永远滞后 1 次。所以再来一遍：
#   · 路径 A（本地 mac）: output/.git 是 clone 过的部署仓 → 直接 commit+push
#   · 路径 B（GH Actions）: $DEPLOY_TMP 临时仓在主部署后被 rm -rf, 这里
#     用同样的 REPO_URL 重新 clone 一份
# ────────────────────────────────────────────────
if [ "$DEPLOY_OK" = true ]; then
    "$PYTHON" -u "$PROJECT_DIR/dashboard_generator.py" >> "$LOG_FILE" 2>&1 || true

    if [ ! -f "$PROJECT_DIR/output/dashboard.html" ]; then
        echo "  ⚠️ dashboard.html 不存在，跳过后置部署" >> "$LOG_FILE"
    elif [ -d "$PROJECT_DIR/output/.git" ]; then
        # 路径 A — 本地 mac，output/ 是 clone 过的部署仓
        # 主部署阶段是用 $DEPLOY_TMP 推的，output/.git 此刻已落后 origin。
        # 先 fetch+reset 同步，避免 non-fast-forward。
        # output/ 全部内容都是部署产物，本地无原创修改，reset 安全。
        cp "$PROJECT_DIR/output/dashboard.html" "$PROJECT_DIR/output/archive/dashboard.html"
        (cd "$PROJECT_DIR/output" && \
         git fetch origin main 2>>"$LOG_FILE" && \
         git reset --hard origin/main 2>>"$LOG_FILE" && \
         git clean -fd \
             -e 'run_health.jsonl' \
             -e '.digest_cache.json' \
             -e 'stats.json.before-*' \
             2>>"$LOG_FILE") || \
            echo "  ⚠️ dashboard 同步 origin 失败" >> "$LOG_FILE"
        # reset 把 dashboard.html 撤回了，重新拷一次
        cp "$PROJECT_DIR/output/dashboard.html" "$PROJECT_DIR/output/archive/dashboard.html" 2>/dev/null || true
        (cd "$PROJECT_DIR/output" && \
         git add dashboard.html archive/dashboard.html 2>/dev/null && \
         git -c user.email="hawaha113@protonmail.com" -c user.name="hawaha112" \
             commit -m "dashboard: post-run update with latest RUN_SUMMARY" 2>>"$LOG_FILE" && \
         git push origin main 2>>"$LOG_FILE") && \
            echo "  📊 dashboard 后置部署完成（含本次 RUN_SUMMARY，路径 A）" >> "$LOG_FILE" || \
            echo "  ⚠️ dashboard 后置部署失败（路径 A，不影响主流程）" >> "$LOG_FILE"
    elif [ -n "$REPO_URL" ]; then
        # 路径 B — GH Actions，重新 clone 部署仓推单文件
        POST_TMP="/tmp/ai_dash_post_$$"
        rm -rf "$POST_TMP"
        if git clone --depth 1 "$REPO_URL" "$POST_TMP" >> "$LOG_FILE" 2>&1; then
            cp "$PROJECT_DIR/output/dashboard.html" "$POST_TMP/dashboard.html"
            mkdir -p "$POST_TMP/archive"
            cp "$PROJECT_DIR/output/dashboard.html" "$POST_TMP/archive/dashboard.html"
            cd "$POST_TMP"
            git config user.email "action@github.com"
            git config user.name "GitHub Action"
            git add dashboard.html archive/dashboard.html 2>/dev/null
            if ! git diff --cached --quiet; then
                if git commit -m "dashboard: post-run update with latest RUN_SUMMARY" >> "$LOG_FILE" 2>&1 \
                   && git push origin main >> "$LOG_FILE" 2>&1; then
                    echo "  📊 dashboard 后置部署完成（含本次 RUN_SUMMARY，路径 B）" >> "$LOG_FILE"
                else
                    echo "  ⚠️ dashboard 后置部署失败（路径 B，push 失败）" >> "$LOG_FILE"
                fi
            else
                echo "  📊 dashboard 后置：无变化，跳过 commit" >> "$LOG_FILE"
            fi
            cd "$PROJECT_DIR"
        else
            echo "  ⚠️ dashboard 后置 clone 失败（路径 B）" >> "$LOG_FILE"
        fi
        rm -rf "$POST_TMP"
    else
        echo "  ⚠️ dashboard 后置部署跳过（无 git 配置）" >> "$LOG_FILE"
    fi
fi

# 清理临时 SSH 私钥(若本次走 SSH 部署)
[ -n "${DEPLOY_SSH_KEYFILE:-}" ] && rm -f "$DEPLOY_SSH_KEYFILE" 2>/dev/null || true

echo "每日出报任务完成" >> "$LOG_FILE"
