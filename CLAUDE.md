# 项目部署与运维快照

> 给后续 Claude session 看的"项目当前状态备忘"。最后更新：2026-05-11。
> 业务/代码细节看 [ai-morning-news/README.md](ai-morning-news/README.md) 和 [ai-morning-news/ARCHITECTURE.md](ai-morning-news/ARCHITECTURE.md)。

---

## 🎯 当前部署架构（一句话）

**仅云端运行**：GitHub Actions cron 是唯一的定时触发源；本地 macOS launchd 任务已 `unload`，但 plist 文件**保留**（随时可挂回）。

笔记本合上/关机/睡眠都不影响每日早报、晚报、周报的生成与推送。

---

## 📅 自动调度时刻表

| Workflow | Cron (UTC) | 北京时间 | 文件 |
|---|---|---|---|
| AI Morning Briefing (早班) | `0 22 * * *` | 06:00 | [.github/workflows/morning-briefing.yml](.github/workflows/morning-briefing.yml) |
| AI Morning Briefing (晚班) | `0 10 * * *` | 18:00 | 同上 |
| AI Weekly Report | `0 10 * * 0` | 周日 18:00 | [.github/workflows/weekly-report.yml](.github/workflows/weekly-report.yml) |
| Dispatch TG | 手动 | — | [.github/workflows/dispatch-tg.yml](.github/workflows/dispatch-tg.yml) |

⚠️ **GitHub Actions cron 是 best-effort**，实测会延迟 30 分钟到几小时不等。已在 [briefing_renderer.py](ai-morning-news/briefing_renderer.py) 内加了"窗口截止时间自动延展到渲染时刻"的保护（commit `2a9e02c`）。

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

### 通用脆弱点
- **LLM JSON 解析**：`llm_analyzer._extract_json` 已处理 markdown 围栏 + 行内换行 + 截断容错，但**值内未转义双引号**只能源头修（prompt 约束）
- **Cloudflare / Nitter 反爬**：[content_fetcher.py](ai-morning-news/content_fetcher.py) 对 X(Twitter) 走 Nitter 实例，经常 429。健康度由 [health_tracker.py](ai-morning-news/health_tracker.py) 跟踪，10+ 连续失败自动停用
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
│   ├── run_daily.sh              主入口（本地+云端共用）
│   ├── run_collect.sh            仅采集阶段
│   ├── config.json               40 个 RSS 源 + LLM 配置 + 去重阈值
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
