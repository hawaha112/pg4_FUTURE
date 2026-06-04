# 项目部署与运维快照

> 给后续 Claude session 看的"项目当前状态备忘"。最后更新：2026-05-30。
> 业务/代码细节看 [ai-morning-news/README.md](ai-morning-news/README.md) 和 [ai-morning-news/ARCHITECTURE.md](ai-morning-news/ARCHITECTURE.md)。

---

## 🎯 当前部署架构（一句话）

**云端 + Anthropic Routine 触发**：早晚班的定时触发改由 [Anthropic Routine](https://claude.ai/code/routines)（remote agent）调用 GitHub workflow_dispatch API 触发；GitHub Actions schedule 已禁用以避免双触发。本地 macOS launchd 已 `unload`（plist 文件保留）。

笔记本合上/关机/睡眠都不影响每日早报、晚报、周报的生成与推送。

---

## 📅 自动调度时刻表

| 触发 | 北京时间 | 触发源 | 备注 |
|---|---|---|---|
| AI Morning Briefing (早班) | 06:00 ± 5-12 分钟 | **Anthropic Routine** `trig_011wSBtNNApn5X4aWEeFUmFq` | 调 workflow_dispatch API |
| AI Morning Briefing (晚班) | 18:00 ± 5-12 分钟 | **Anthropic Routine** `trig_01XkWy5x1jWvE3yNaEWbb1AP` | 调 workflow_dispatch API |
| AI Weekly Report | 周日 18:00 ± 5-12 分钟 | **Anthropic Routine** `trig_011CqZKH3cDC55G5CRcyijU7` | 调 workflow_dispatch API |
| AI 突发热点检测 | 每小时 (:15) | **GitHub Actions cron** | [breaking-news.yml](.github/workflows/breaking-news.yml) — 纯 stdlib 扫 HN/HF, 零 Claude token, **不要挂 routine** |
| Daily Ops 自检 | 每日 09:30 | GitHub Actions cron | [daily-ops.yml](.github/workflows/daily-ops.yml) → 链式触发 auto-fix-sources |
| Dispatch TG | 手动 | — | [.github/workflows/dispatch-tg.yml](.github/workflows/dispatch-tg.yml) |

**实测准时性**：Anthropic Routine 早班 5/13/14/15 都在 06:05-06:12 触发（精度 5-12 分钟），对比之前 GH Actions schedule 的 30-90 分钟延迟改善显著。

⚠️ **Routine 只给"需要精准时间"的早班/晚班/周报用**（一天 ~2 次）。突发检测、daily-ops 一律走 GitHub Actions cron —— **cron 不消耗 Claude routine 配额**。曾经有个**每 2h 触发 breaking-news 的 routine**（一天 12 次 → routine 用量超标），2026-05-30 确认应删除：breaking-news.yml 自带每小时 cron 兜底，detector 纯 stdlib 不烧 token，根本不需要 routine。**新增"扫描类"workflow 默认用 cron，不要随手挂 routine。**

⚠️ Routine 内部用 fine-grained PAT 调 GH API（PAT `claude-routine-pg4future-trigger`，权限 Actions: read/write，scope pg4_FUTURE，**30 天过期** — 6/14/2026 需续期）。

⚠️ **GitHub Actions schedule 已注释**（[morning-briefing.yml:29-35](.github/workflows/morning-briefing.yml)），原因避免双触发。但 [briefing_renderer.py](ai-morning-news/briefing_renderer.py) 的"窗口截止时间自动延展"（commit `2a9e02c`）保留作为兜底防御 — 如果未来某天 routine 也延迟，仍能正确处理。

---

## 🔑 Secrets 与凭证

5 个 secrets 都在 GitHub repo（`hawaha112/pg4_FUTURE`），通过 `gh secret list` 可见：

| Secret | 用途 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude LLM API（用 `claude setup-token` 生成；过期就 401 全线挂） |
| `TG_BOT_TOKEN` | Telegram Bot |
| `TG_CHAT_ID` | Telegram 推送频道 |
| `BRIEFING_URL` | TG 消息里附带的早报链接 |
| `DEPLOY_REPO_TOKEN` | 推到部署仓 `hawaha112/ai-morning-briefing` 的 PAT |

**token 过期续期**：
```bash
claude setup-token                                                        # 本地生成新 token
gh secret set CLAUDE_CODE_OAUTH_TOKEN -R hawaha112/pg4_FUTURE             # 更新到 GH
```

run_daily.sh 启动时会跑 `Probe LLM token (fail-fast on 401)`，token 失效会立刻发 TG 告警。

### 🔁 Anthropic Routine 用的 GitHub PAT（独立于上面 5 个 secret）

Routine 调 `workflow_dispatch` API 需要的 PAT **嵌入在 routine prompt 里**（不是 GH secret）：

| 字段 | 值 |
|---|---|
| 名称 | `claude-routine-pg4future-trigger` |
| 类型 | Fine-grained PAT |
| 权限 | Actions: Read and write + Metadata: Read-only |
| 仓库 scope | hawaha112/pg4_FUTURE |
| 过期 | 2026-06-14（30 天，**到期前需续期**） |
| 管理位置 | https://github.com/settings/personal-access-tokens |

**续期流程**：
1. 在 https://github.com/settings/personal-access-tokens 找到 `claude-routine-pg4future-trigger`
2. Regenerate（或新建一个同样配置的 PAT）
3. 在 https://claude.ai/code/routines 编辑两个 routine 的 prompt，把里面的 `Authorization: Bearer github_pat_...` 替换为新 PAT

---

## 💾 状态持久化

- `briefing-state` 分支（孤儿、永远只有 1 commit）保存 `events.db / dedup.db / llm_cache.db / source_health.json`
- 每次 workflow 跑完自动 force-push 更新
- 同时打 `snapshot/YYYYMMDD-HHmm-{am,pm}` tag，**保留 7 天回滚窗口**

```bash
git fetch origin briefing-state                                    # 拉最新状态
git fetch origin 'refs/tags/snapshot/*:refs/tags/snapshot/*'       # 拉所有快照
```

---

## 🖥️ 本地 launchd（已停用但保留）

plist 文件位置：`~/Library/LaunchAgents/com.hawaha.ai-{collector,morning-briefing}.plist`

**当前状态：已 unload（不跑）**

```bash
# 验证当前状态
launchctl list | grep hawaha     # 当前应该为空

# 想重启本地（云端崩了兜底 / 本地调试）
launchctl load ~/Library/LaunchAgents/com.hawaha.ai-collector.plist
launchctl load ~/Library/LaunchAgents/com.hawaha.ai-morning-briefing.plist

# 想再停掉
launchctl unload ~/Library/LaunchAgents/com.hawaha.ai-collector.plist
launchctl unload ~/Library/LaunchAgents/com.hawaha.ai-morning-briefing.plist
```

### ⚠️ 重新启用本地前必须做的事

云端 cron 也是开着的，**直接挂回本地会双重推送 + 双重部署 + state 互覆盖**。

启用本地前要先**关掉云端 cron**：注释 [.github/workflows/morning-briefing.yml](.github/workflows/morning-briefing.yml) 里的 `schedule:` 两行后 push。或者反过来，永久切回本地的话，把整段 schedule 删掉。

---

## 📺 常用运维命令 cookbook

### 看云端运行历史
```bash
gh run list --repo hawaha112/pg4_FUTURE --workflow morning-briefing.yml --limit 10
gh run list --repo hawaha112/pg4_FUTURE --workflow weekly-report.yml --limit 5
```

### 看某次运行的失败详情
```bash
gh run view <run-id> --repo hawaha112/pg4_FUTURE
gh run view --job <job-id> --repo hawaha112/pg4_FUTURE --log         # 完整日志
```

### 手动触发
```bash
# 早班 / 晚班 / 自动判断
gh workflow run morning-briefing.yml --repo hawaha112/pg4_FUTURE --ref main \
  -f shift=am -f backfill_polluted_hours=0 -f silent_tg=false
# 手动周报
gh workflow run weekly-report.yml --repo hawaha112/pg4_FUTURE --ref main
```

⚠️ **手动 dispatch am/pm 在非班次时刻跑，窗口会扩展到 _now**（cron-延迟保护机制副作用），可能产出范围远大于预期的"半天窗口"。仅作测试用，不要替代正常 cron。

### 下载某次运行的产物
```bash
gh run download <run-id> --repo hawaha112/pg4_FUTURE
# 产物结构：briefing-{am,pm}-N/daily_run.log + output/{index.html,stats.json,...}
```

### 静默测试模式
workflow_dispatch 时把 `silent_tg=true`，跳过 TG 推送但完整跑流水线 + 部署到 Pages，避免轰炸 TG 频道。

---

## 🐛 已知坑 + 最近修复

### 修复 1: weekly_report JSON 解析炸（commit `fd93fc6`）

LLM 偶尔把 JSON 包在 ` ```json ... ``` ` 里、或在字符串值里塞 ASCII 双引号（如 `从"卖GPU"转向"算力"`），导致解析失败。

修复方式：
1. [weekly_report.py:169](ai-morning-news/weekly_report.py) prompt 加约束 — 嵌套引用必须用「」/『』
2. [weekly_report.py:204](ai-morning-news/weekly_report.py) 加一次性重试 — 解析失败时附更严格提示再问一次

### 修复 2: 早班漏 90% 内容（commit `2a9e02c`）

[briefing_renderer.py:185](ai-morning-news/briefing_renderer.py) 班次窗口截止时间写死 `am=06:00 / pm=18:00`，但 GH cron 延迟 30-60 分钟，渲染时刻 `06:48`，结果 06:00-06:48 间发布的最新热点全被"窗口外"过滤。2026-05-11 早班实测 28→3 条。

修复：`if _now > window_end: window_end = _now`。起点不动，避免破坏 230 行注释保护的"严格只放行窗口内首发事件"语义（旧 bug：04-28 文章混进 04-29 晚报）。

### 修复 3: TG 早报"单条更新"防堆积（commit `538066e`, 2026-05-30）

旧逻辑每班 `sendMessage` 新发一条、从不删旧 → TG 窗口里早报越堆越多（用户反馈"消息爆炸 / 链接不更新"）。改成 [run_daily.sh](ai-morning-news/run_daily.sh) 每班**先 `deleteMessage` 上一条、再发新的**，message_id 存 `tg_state.json`（经 briefing-state 分支跨班次持久化，restore 的 `*.json` 已覆盖、push-back 列表已加）。窗口里只保留一条早报、每班刷新；部署失败时**不删**上一条可用早报、单独告警。`send_tg_capture` 的 curl 加了 `|| resp=""` 兜底（`set -e` 下网络失败不致整脚本在已部署后崩退）。

### 修复 4: 突发检测阈值校准 — 旧值永不触发（commit `538066e`, 2026-05-30）

[breaking_news_detector.py](ai-morning-news/breaking_news_detector.py) 旧阈值 `HN≥800 分 / 只看最近 2h` 实测**永不触发**：HN 故事要 6-12h 才攒够分，最近 2h 内 AI 故事常 0 条（workflow 日志每次"HN 抓到 0 条 → 无突发"）。用户"从没收到突发"即此因。改成 **24h 窗口 + HN≥300 / HF≥2000**，加单次上限 5 + 去重 48h，全部 env 可调：`BREAKING_HN_POINTS` / `BREAKING_HN_HOURS` / `BREAKING_HF_LIKES` / `BREAKING_DEDUP_TTL_HOURS` / `BREAKING_MAX_PER_RUN`。

**"只要大事"事件过滤（同 commit 续）**：实测发现 HN 高分 ≠ 大事（观点帖"Please Use AI" 724 分也上榜）。给 HN 加了一道**新闻事件标题过滤** `_HN_EVENT_RE`（只放行发布/融资/收购/事故/带版本号型号，滤掉观点/讨论/提问帖），由 `BREAKING_HN_EVENT_ONLY`(默认 true) 控制；设 false 退回"所有热门 HN 都推"。HF trending 本身就是真模型发布，不过此闸。

### 修复 5: 归档页"实体追踪"chip 404（2026-05-31）

实体追踪 chip 的 href 是相对**主页根目录**的 `entities/{id}-30d.html`（[html_generator.py:799](ai-morning-news/html_generator.py)）。主页点正常（实测 200），但归档页在 `archive/` 子目录下，同样的相对链接解析成 `archive/entities/...` → **404**——从 TG「本班次归档」进去再点实体追踪就打不开。根因同 dashboard 链接：归档页是 index.html 的 sed 副本，dashboard 链接当初改对了、entity chip 漏了。修复：[run_daily.sh](ai-morning-news/run_daily.sh) 归档 sed 增加 `href="entities/` → `href="../entities/"`（管新归档页）；**已部署的 29 个旧归档页一次性回填**（部署仓 commit `244257e`，perl 同款改写，幂等）。

**配套 UX (同批)**：实体页「← 返回早报」原来写死 `../index.html`（总回最新主早报，不是来路）。改成 `history.back()` 智能返回——来自归档页就回那张归档页、来自主页就回主页，无 referrer/直接打开 时回退 `../index.html`，JS 关掉有 href 兜底（[entity_timeline_generator.py](ai-morning-news/entity_timeline_generator.py)）。12 个已部署实体页同批注入 onclick。

### 改进 6: TG 推送内容优先 + 页面信息重排（2026-05-31，产品审查）

1. **TG 早报消息内容优先**：原来推的是「收录 N 条 · LLM% · 源健康」纯流水线诊断、链接还跳仪表盘（撞用户"只要内容不要诊断"的偏好）。改成**前置 important_events 头条（最多 3 条，第一条带 ⭐）+ 共 N 条 + 直达全文链接**；砍掉覆盖率/源健康/用时（那些进仪表盘）。头条由 [_tg_headlines.py](ai-morning-news/_tg_headlines.py) 从 stats.json 提取（独立脚本，避免内联 heredoc 在云端静默失败）。安静日无 importance≥4 事件时优雅退化为「📰 标题 / 共 N 条 · 阅读全文」。仅保留 `NO_LLM` 一条降级告警。
2. **页面信息重排**：[templates/page.html](ai-morning-news/templates/page.html) 把"实体追踪"12-chip 从『判断与必读之间』挪到**所有新闻下方**（二级导航不挤占头部）。新顺序：今日判断 → 今日必读 → 其他资讯 → 大V → 实体追踪。
3. 突发消息的"命中信号"（HN 分 / HF 赞）**本就已在** [breaking_news_detector.py](ai-morning-news/breaking_news_detector.py) 的消息里，无需改。

### 改进 7: 突发推送翻译卡片 + HN AI 过滤修词边界（2026-05-31）

1. **突发消息从"英文标题+链接"→翻译卡片**：[breaking_news_detector.py](ai-morning-news/breaking_news_detector.py) 加 `_translate_title`（调 claude proxy 把外文标题译中文），消息改成卡片【中文标题 + 英文原标题(便于核对) + 命中信号(HN 分/HF 赞) + 链接】；翻译失败回退英文原标题，**永不阻塞推送**。
2. **两段式省 token/Actions**：detector 拆 `detect`(纯 stdlib, 每小时, 无命中即止) / `push`(读 pending 翻译后推) / `all`(本地兜底)。[breaking-news.yml](.github/workflows/breaking-news.yml) 改成 detect 先跑 → **仅当有命中**才装 claude + 起 proxy + push。无突发的小时(绝大多数)不装 claude、不调 LLM → token 几乎为 0、Actions 分钟也省。timeout 5→10 分。新增 `min_points` dispatch 输入(临时降阈做测试/演示)。
3. **HN AI 过滤修词边界 bug**：[hot_signals.py](ai-morning-news/extractors/hot_signals.py) 旧 `'ai' in title` 子串匹配会命中 "br**ai**n"/"ch**ai**r"/"**ai**r"，把非 AI 帖(实测"Creatine raises brain energy" 518 分)误判成 AI 突发。改成词边界正则（`\bai\b`/`\brag\b` 要求整词，其余允许前缀）。

### 改进 8: 判断卡"已核/未核"徽章 → 原文来源链接（2026-06-01）

用户反馈"已核/未核"对读者太抽象。把「今日判断」卡顶部的事实核对计数徽章（`✓ N 处已核 / M 处未核`）**换成卡片底部的原文来源链接**：`evidence_ids` → `all_items[i]` 的真实 URL（[html_generator.py](ai-morning-news/html_generator.py) 判断卡循环；`generate_digest(all_items)` 与 `generate_html(all_items,…)` 收同一份 list，下标对齐，安全映射）。形如「📎 来源：🤖 OpenAI Blog · 📰 Techmeme」，点开即核对。**保留** `⚠ N 处与新闻不符`（contradicted_count>0，真红旗）。CSS 加 `.jc-sources`/`.jc-src`。下一班出报生效。

### 改进 9: 内容质量三连（2026-06-01）

实测真实产出后定的（判断/deep_analysis 已很强，没动）：
1. **why_it_matters 去套话**（commit `93713d4`）：[prompts/system.txt](ai-morning-news/prompts/system.txt) 的 why_it_matters 原只"说清所以呢"、无反套话约束，输出退回"标志着…关键一步"。补全禁忌词 + 要求具体后果（谁受益谁受损）+ 强弱对照例。
2. **渲染前 LLM 语义去重**（commit `3c2b667`）：同一事件不同来源/措辞，表层相似度抓不住（实测真重复 minhash 仅 0.28，调阈值必漏/误合并）。新增 `LLMAnalyzer.dedupe_same_event`（出报前把当天标题丢 LLM 判"哪些同事件"并合并，保留 importance 最高那条，失败/越界一律原样返回），[briefing_renderer.py](ai-morning-news/briefing_renderer.py) 在 digest 前调用 → 去重后 all_items 同时供判断和卡片。
3. **今日口播稿**：判断是为"读"写的、念出来太密（用户要做音频/视频）。新增 `LLMAnalyzer.generate_broadcast_script`（判断→60-90 秒口语稿），页面底部加可复制「🎙 今日口播稿」区块（[page.html](ai-morning-news/templates/page.html) `$broadcast_html` + html_generator + style.css）。

三项均 LLM 依赖、仅命中/出报时调用（token 小）；逻辑/渲染已本地桩测，最终文案需云端出报验证。

### 改进 10: 突发并进早报、防轰炸（2026-06-02）

用户反馈突发逐条推 = 消息轰炸；要"只 2 个链接（早晚报 + 突发并进早报）"。最终方案：
- **突发命中** → 翻译累积进 `pushed_breaking`（含 `title_zh`+`signal`）→ `push` 模式写 `output/breaking.json`（近 24h 事件数组，`_breaking_payload`）+ `breaking_push.flag`(新增数 总数)。不再逐条推 TG、不再渲染独立 HTML 页。
- **[breaking-news.yml](.github/workflows/breaking-news.yml) 条件步骤**（仅 `NEW>0`）：部署 `breaking.json` → 部署仓 `archive/breaking.json`（走 archive/** 白名单）；**删旧推新一条** TG「🚨 AI 突发 · 近24h N 条」，**链接指向早报**（msg_id 存 `breaking_tg_state.json`，cache 持久化）。
- **早报页前端**（[page.html](ai-morning-news/templates/page.html) `#breaking-banner` + [script.js](ai-morning-news/templates/script.js) IIFE）：打开早报时 `fetch('archive/breaking.json')`，有近 24h 突发就渲染**顶部「🚨 突发」卡片区**（判断之前）。**始终拉最新** → 早晚班之间有突发、随时打开早报都能看到，不必等下一班出报。
- 结果：**TG 只 2 条消息（早晚报 + 突发提醒），1 个页面/URL（早报）**；突发卡片在早报顶部。
- 窗口可调 `BREAKING_DISPLAY_HOURS`(默认 24)。`_render_breaking_html` 弃用保留。`demo` 仍走旧单卡推送(翻译自检)。
- ⚠️ 坑：`run: |` 块里**别写多行 `python3 -c`**——续行顶格会被 YAML 当 mapping 解析报错。用单行。
- ⚠️ 坑2（2026-06-03 踩）：`read NEW TOTAL < flag` 在 flag **无结尾换行**时撞 EOF 返回非零 → `bash -e` 误杀整步骤（push 已写 breaking.json 但部署/推送不发生）。修复：[breaking_news_detector.py](ai-morning-news/breaking_news_detector.py) flag 写入带 `\n` + [breaking-news.yml](.github/workflows/breaking-news.yml) `read … || true` 双保险。
- **噪声闸 `_NOISE_RE`（2026-06-03）**：事件闸之前先否掉问句（`?`结尾 / why·how·when 等疑问词开头）、观点（says/claims/predicts/argues/warns…）、估值表态（`aren't worth`/`worth $`/overvalued/bubble）、观察猜测（`appears to`/`seems to`）、讨论帖（weird/`vs`/thoughts on/`i built…`）。实测 `min_points=10` 时这类帖蹭动作词或型号名混进来。过滤跑在**翻译前的英文原标题**上，故以英文标记为主。`BREAKING_HN_EVENT_ONLY=false` 时连同事件闸一起关。
- **事件正则词边界 bug（2026-06-03 同批）**：`_HN_EVENT_RE`/`_EVENT_ACTION_RE` 的短词缺两侧 `\b` → 子串误命中：`ships?`命中"cen**sorship**"（"Minimax 无审查"误判突发）、`raise`命中"p**raise**"、`sue`命中"is**sue**s"。全部加 `\b`。另把 `\$\d`（裸金额，命中"$1"）收紧成 `\$\d+(?:[.,]\d+)?\s?(?:k|m|b|t|bn|million|billion|trillion)\b`（必须带量级，滤掉价格）。单测：8 噪声/子串全挡、8 真事件（含 ship/raise/sue/acquire/outage）全放行。
- **signal/中文标题在 DB 丢字段（2026-06-03 核心 bug）**：突发卡片 `signal`（"HN 89 分"）全空、中文标题退回英文。根因：breaking-news.yml **不带 events.db**（gitignored、注释"不依赖"）→ 每跑用全新空 DB → `_load_pushed()` 先读 JSON、但 `_accumulate_signals` 同时把 poor 记录写进新空 DB（旧 `mark_breaking_pushed` 只存 source/title/url/score）→ `_breaking_events_in_window` 再 `_load_pushed()` 发现 DB 非空就返回 **poor DB 数据**。修复：[state_store.py](ai-morning-news/state_store.py) `pushed_breaking` 加 `title_zh`/`signal` 两列（含对老 briefing-state DB 的幂等 `ALTER TABLE` 补列）+ `mark_breaking_pushed` 存这俩；detector `_mark_pushed_one`/`_accumulate_signals`/JSON→DB 迁移全程透传。**坑**：去重过滤在 `_detect_and_select`（`_id in pushed`），已推事件 re-run 时 `hits=0` → 不重部署，故富化后的 signal 要等"新事件"或清 actions/cache 才上线。
- **前端客户端 24h 过滤（2026-06-03）**：[script.js](ai-morning-news/templates/script.js) banner 渲染前按 `e.ts` 再滤一道 24h —— 即便 `breaking.json` 滞后未重部署，页面也绝不显示过期突发、全过期自动隐藏。url 顺手补 `escHtml`。

### 改进 11: 早报页信息架构重排（2026-06-04，用户反馈"没层次/记不住"）

用户反馈：突发放在判断（看板）之前怪、必读卡片没归类、整页没逻辑层次记不住。三处改（仅模板层，逻辑未动）：
1. **突发移到判断之后**（[page.html](ai-morning-news/templates/page.html)）：`#breaking-banner` 从页面最顶（判断之前）移到 `#sec-breaking`（判断之后）。判断仍是全天头条/结论先行，突发作为"近24h时效提醒"紧随、不抢头条。
2. **「今日必读」也按主题分组**（[html_generator.py](ai-morning-news/html_generator.py)）：原本是平铺 `featured-grid`，现与"更多资讯"统一——`_GRID_CAT_ORDER`/`_cat_rank` 上移到 featured block 之前两处共用；必读按 `categories[0]` 分桶，每类一个 `grid-cat-head` 小标题 + 各自的 `featured-grid`。**data-idx 仍按 `enumerate(all_items)`**，分组只重排卡片 HTML，modal 不错位（已验证 0-5 对齐）。
3. **顶部「今日导览」+ 版块锚点 + 编号顺序**：`$today_nav` 按"非空版块"生成跳转 chip（🎯判断 🚨突发 ⭐必读 📚更多 👤大V 🔗实体 🎙口播），每个 `<section id="sec-*" class="page-sec">` 做锚点。突发 chip 默认 `hidden`，由 [script.js](ai-morning-news/templates/script.js) 在确有近24h突发时点亮（与 banner 同步）。CSS `.today-nav`/`.tn-link`/`.tn-breaking` + `.page-sec{scroll-margin-top:84px}`（清 sticky header）。
- "其他资讯"标题改"📚 更多资讯 · 按主题分类"。`grid-cat-head` 在 featured-section(非 grid 容器)里 `grid-column` 无副作用、`display:flex` 正常渲染，两处复用同款。

### 通用脆弱点
- **LLM JSON 解析**：`llm_analyzer._extract_json` 已处理 markdown 围栏 + 行内换行 + 截断容错，但**值内未转义双引号**只能源头修（prompt 约束）
- **Cloudflare / Nitter 反爬**：[content_fetcher.py](ai-morning-news/content_fetcher.py) 对 X(Twitter) 走 Nitter 实例，经常 429。健康度由 [health_tracker.py](ai-morning-news/health_tracker.py) 跟踪，10+ 连续失败自动停用
- **云端必须装 `curl_cffi`**（2026-06-02 修）：[tls_client.py](ai-morning-news/tls_client.py) 靠 curl_cffi 模拟 Chrome TLS 指纹绕 Cloudflare/Substack；不装则回退裸 urllib，Substack（Import AI 等）/严审站间歇 403。本地有、云端 workflow 原来只 `pip install certifi` 漏了它 → 已给 [morning-briefing.yml](.github/workflows/morning-briefing.yml) + [auto-fix-sources.yml](.github/workflows/auto-fix-sources.yml) 补 `pip install curl_cffi`（best-effort，装不上回退）。新增采集类 workflow 记得带上。
- **节点 20 deprecated 警告**：`actions/checkout@v4` 等 2026-09-16 后会强制 Node 24。不影响功能但要在那之前升级

---

## 📂 文件地图

```
.
├── ai-morning-news/              主项目 (Python 3.10+)
│   ├── collector.py              Stage A: RSS 抓取 → 去重 → LLM → 聚类 → 入库
│   ├── briefing_renderer.py      Stage B: 读事件 → 渲染 HTML → stats
│   ├── html_generator.py         Jinja-like 模板渲染
│   ├── dashboard_generator.py    独立的 dashboard.html（KPI/源健康/历史趋势）
│   ├── event_store.py            SQLite 事件库（canonical_events + evidence）
│   ├── llm_analyzer.py           LLM 调用 + JSON 提取 + 重试
│   ├── dedup_engine.py           MinHash/LSH 去重（中英分阈值）
│   ├── event_cluster.py          确定性签名预合并 + LSH 聚类
│   ├── weekly_report.py          周日周报
│   ├── breaking_news_detector.py 突发热点检测（扫 HN/HF/Reddit 热度, 纯 stdlib, 每小时 cron）
│   ├── run_daily.sh              主入口（本地+云端共用）
│   ├── run_collect.sh            仅采集阶段
│   ├── config.json               87 个源（23 个 X via Nitter + 10 个 GitHub releases.atom + RSS）+ LLM + 去重（全免账号）；突发另扫 HN/HF/Reddit×5 子版
│   ├── prompts/                  抽取出的 prompt 模板
│   ├── causal_kb/                因果链知识库 JSON
│   └── tests/                    pytest 测试
├── .github/workflows/            云端 cron
├── docs/                         项目介绍 / 优化建议 / 信源审计
├── index.html / modal_data.js    根目录是当日早报的渲染产物（生成物，不要手改）
└── CLAUDE.md                     ← 本文件
```

部署仓（独立）：[hawaha112/ai-morning-briefing](https://github.com/hawaha112/ai-morning-briefing) → GitHub Pages

---

## 🚦 健康检查清单（任何时候想确认"一切正常"跑一遍）

```bash
# 1. 云端 workflow 都 active
gh workflow list --repo hawaha112/pg4_FUTURE

# 2. 最近 5 次运行都 success
gh run list --repo hawaha112/pg4_FUTURE --workflow morning-briefing.yml --limit 5

# 3. secrets 齐全（5 个）
gh secret list --repo hawaha112/pg4_FUTURE

# 4. 本地 launchd 静默
launchctl list | grep hawaha   # 应该为空

# 5. 部署仓在更新
gh api repos/hawaha112/ai-morning-briefing/commits --jq '.[0:3] | .[] | "\(.commit.author.date) \(.commit.message[0:60])"'
```

任一项不符合就是异常信号。
